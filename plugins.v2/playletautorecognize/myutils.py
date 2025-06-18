import random
import chardet
import shutil
import subprocess
import threading
import time
import re
import requests
import pickle
import traceback

from threading import RLock
from pathlib import Path
from typing import Optional, Any, List, Dict, Tuple, Union

from lxml import etree
from xpinyin import Pinyin

from app.helper.sites import SitesHelper
from app.core.meta import MetaBase
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.event import eventmanager, Event
from app.modules import _ModuleBase
from app.modules.filemanager import FileManagerModule
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo
from app.schemas.types import EventType, MediaType, NotificationType, ModuleType, MediaRecognizeType
from app.schemas import TransferInfo, ExistMediaInfo, TmdbEpisode, TransferDirectoryConf, FileItem, StorageUsage
from app.utils.system import SystemUtils
from app.utils.http import RequestUtils


lock = RLock()

CACHE_EXPIRE_TIMESTAMP_STR = "cache_expire_timestamp"
EXPIRE_TIMESTAMP = settings.CONF["meta"]

class PlayletCache():
    """
    短剧缓存数据
    {
        "title": '',
        "year": '',
        "detail_link": '',
    }
    """
    _meta_data: dict = {}
    # 缓存文件路径
    _meta_path: Path = None
    # TMDB缓存过期
    _tmdb_cache_expire: bool = True

    def __init__(self):
        self._meta_path = settings.TEMP_PATH / "__playlet_cache__"
        self._meta_data = self.__load(self._meta_path)

    def clear(self):
        """
        清空所有TMDB缓存
        """
        with lock:
            self._meta_data = {}

    @staticmethod
    def __get_key(meta: MetaBase) -> str:
        """
        获取缓存KEY
        """
        return f"{meta.cn_name}-{meta.year}-{meta.begin_season}"

    def get(self, meta: MetaBase):
        """
        根据KEY值获取缓存值
        """
        key = self.__get_key(meta)
        with lock:
            info: dict = self._meta_data.get(key)
            if info:
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire or int(time.time()) < expire:
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                    self._meta_data[key] = info
                elif expire and self._tmdb_cache_expire:
                    self.delete(key)
            return info or {}

    def delete(self, key: str) -> dict:
        """
        删除缓存信息
        @param key: 缓存key
        @return: 被删除的缓存内容
        """
        with lock:
            return self._meta_data.pop(key, {})

    @staticmethod
    def __load(path: Path) -> dict:
        """
        从文件中加载缓存
        """
        try:
            if path.exists():
                with open(path, 'rb') as f:
                    data = pickle.load(f)
                return data
            return {}
        except Exception as e:
            logger.error(f"加载缓存失败: {str(e)} - {traceback.format_exc()}")
            return {}

    def update(self, meta: MetaBase, data) -> None:
        """
        新增或更新缓存条目
        """
        with lock:
            if data:
                # 缓存标题
                cache_title = data.title
                # 缓存年份
                cache_year = data.year
                # 海报

                self._meta_data[self.__get_key(meta)] = {
                        "year": cache_year,
                        "title": cache_title,
                        "data": data,
                        CACHE_EXPIRE_TIMESTAMP_STR: int(time.time()) + EXPIRE_TIMESTAMP
                    }

    def save(self, force: Optional[bool] = False) -> None:
        """
        保存缓存数据到文件
        """

        meta_data = self.__load(self._meta_path)
        new_meta_data = self._meta_data

        if not force \
                and not self._random_sample(new_meta_data) \
                and meta_data.keys() == new_meta_data.keys():
            return

        with open(self._meta_path, 'wb') as f:
            pickle.dump(new_meta_data, f, pickle.HIGHEST_PROTOCOL) # noqa

    def _random_sample(self, new_meta_data: dict) -> bool:
        """
        采样分析是否需要保存
        """
        ret = False
        if len(new_meta_data) < 25:
            keys = list(new_meta_data.keys())
            for k in keys:
                info = new_meta_data.get(k)
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire:
                    ret = True
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                elif int(time.time()) >= expire:
                    ret = True
                    if self._tmdb_cache_expire:
                        new_meta_data.pop(k)
        else:
            count = 0
            keys = random.sample(sorted(new_meta_data.keys()), 25)
            for k in keys:
                info = new_meta_data.get(k)
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire:
                    ret = True
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                elif int(time.time()) >= expire:
                    ret = True
                    if self._tmdb_cache_expire:
                        new_meta_data.pop(k)
                        count += 1
            if count >= 5:
                ret |= self._random_sample(new_meta_data)
        return ret

    def get_title(self, key: str) -> Optional[str]:
        """
        获取缓存的标题
        """
        cache_media_info = self._meta_data.get(key)
        if not cache_media_info:
            return None
        return cache_media_info.get("title")

    def set_title(self, key: str, cn_title: str) -> None:
        """
        重新设置缓存标题
        """
        cache_media_info = self._meta_data.get(key)
        if not cache_media_info:
            return
        self._meta_data[key]['title'] = cn_title



class PlayletScraper():
    pass



def chinese_season_to_number(chinese_season):
    '''
    转换中文季为数字
    :param chinese_season: 中文季字符串
    :return 数字格式的季
    '''
    chinese_season = chinese_season.replace('第', '').replace('季', '')
    chinese_numbers = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]

    # 将中文数字替换为阿拉伯数字
    for idx, num in enumerate(chinese_numbers):
        chinese_season = chinese_season.replace(num, str(idx))

    number_str = ""
    for idx, char in enumerate(chinese_season):
        if char.isdigit():
            number_str += char
        elif char == "十":
            if idx == 0 and idx == len(chinese_season) - 1:
                number_str += "10"
            elif idx == 0:
                number_str += "1"  # 如果在第一位，转为1
            elif idx == len(chinese_season) - 1:
                number_str += "0"  # 如果在最后一位，转为0

    try:
        number = int(number_str)
    except:
        number = 1

    return number

def get_page_source(url: str, session = None, cookies = None, proxies = None, timeout: int = 5):
    """
    获取页面资源
    """
    logger.debug(f"请求网页：{url}")
    ret = RequestUtils(
        session=session,
        cookies=cookies,
        proxies=proxies,
        timeout=timeout,
    ).get_res(url, allow_redirects=True)
    if ret is not None:
        # 使用chardet检测字符编码
        raw_data = ret.content
        if raw_data:
            try:
                result = chardet.detect(raw_data)
                encoding = result['encoding']
                # 解码为字符串
                page_source = raw_data.decode(encoding)
            except Exception as e:
                # 探测utf-8解码
                if re.search(r"charset=\"?utf-8\"?", ret.text, re.IGNORECASE):
                    ret.encoding = "utf-8"
                else:
                    ret.encoding = ret.apparent_encoding
                page_source = ret.text
        else:
            page_source = ret.text
    else:
        page_source = ""

    #logger.debug(f"请求网页：{page_source}")
    return page_source

def _to_pinyin_with_title(s):
    if not s:
        return ""

    p = Pinyin()
    pinyin_list = []
    for z in s:
        pinyin_list.append(p.get_pinyin(z, '').title())

    return ' '.join(pinyin_list).replace('，', ',')

def meta_search_tv_name(file_meta, tv_name: str):
    tv_name = tv_name.strip('.')
    tv_name = re.sub(r'^\d+[-.]*', '', tv_name)
    tv_name = re.sub(r'（', '(', re.sub(r'）', ')', tv_name))
    tv_name = re.sub(r'＆', '&', tv_name)
    logger.info(f"尝试识别媒体信息：{tv_name}")
    match = re.match(r'^(.*?)\(([全共]?\d+)[集话話期幕][全完]?\)(?:&?([^&]+))?', tv_name)
    if match:
        title = match.group(1).strip().split('(')[0]
        try:
            episodes = int(match.group(2))
        except:
            episodes = 0
        actors = match.group(3)
        if actors and not '剧' in actors:
            actors = actors.replace('/', ' ').strip()
            if '&' in actors:
                actor_list = actors.split('&')
            else:
                actor_list = [actor.strip() for actor in actors.split() if len(actor) <= 4]
        else:
            actor_list = []
    else:
        title = tv_name.split('(')[0]
        episodes = 0
        actor_list = []

    if '-' in file_meta.org_string:
        ep_match = re.search(r'(\d+)-(\d+)', file_meta.org_string)
        logger.info(f"从文件名中提取集数：{ep_match}")
        if ep_match:
            try:
                file_meta.begin_episode = int(ep_match.group(1))
                file_meta.end_episode = int(ep_match.group(2))
            except:
                logger.error(f"文件名获取的集数错误({ep_match.group(1)}-{ep_match.group(2)})")

    if actor_list:
        actors = ' '.join(actor_list)
        subtitle = f"{title} | 演员：{actors}"
    else:
        subtitle = tv_name

    file_meta.org_string = title
    file_meta.subtitle = subtitle
    file_meta.cn_name = title
    file_meta.en_name = _to_pinyin_with_title(title)
    file_meta.begin_season = 1
    file_meta.total_season = 1
    file_meta.total_episode = episodes
    logger.info(f"file_meta={file_meta}")
    return file_meta
