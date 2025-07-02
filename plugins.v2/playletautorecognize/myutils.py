import random
import chardet
import time
import re
import pickle
import traceback

from threading import RLock
from pathlib import Path
from typing import Optional

from app.core.meta import MetaBase
from app.core.config import settings
from app.core.context import MediaInfo
from app.log import logger
from app.utils.http import RequestUtils


lock = RLock()

CACHE_EXPIRE_TIMESTAMP_STR = "cache_expire_timestamp"
CONF = settings.CONF
if isinstance(CONF, dict):
    EXPIRE_TIMESTAMP = CONF["meta"]
else:
    EXPIRE_TIMESTAMP = CONF.meta

class PlayletCache():
    """
    短剧缓存数据
    {
        "title": '',
        "year": '',
        "error": 0,
        "detail_link": '',
    }
    """
    _meta_data: dict = {}
    # 缓存文件路径
    _meta_path: Path = None
    # TMDB缓存过期
    _cache_expire: bool = True

    def __init__(self, name):
        self._meta_path = settings.TEMP_PATH / f"__playlet_{name}_cache__"
        self._meta_data = self.__load(self._meta_path)

    def clear(self):
        """
        清空所有缓存
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
                elif expire and self._cache_expire:
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
                        "error": 0,
                        "data": data,
                        CACHE_EXPIRE_TIMESTAMP_STR: int(time.time()) + EXPIRE_TIMESTAMP
                    }
            else:
                cache_title = meta.cn_name if meta.cn_name else meta.en_name
                if self._meta_data.get(self.__get_key(meta)):
                    error = self._meta_data.get(self.__get_key(meta)).get('error') + 1
                else:
                    error = 1
                self._meta_data[self.__get_key(meta)] = {
                        "year": None,
                        "title": cache_title,
                        "error": error,
                        "data": None,
                        CACHE_EXPIRE_TIMESTAMP_STR: int(time.time()) + EXPIRE_TIMESTAMP
                    }

    def save(self, force: Optional[bool] = False) -> None:
        """
        保存缓存数据到文件
        """

        meta_data = self.__load(self._meta_path)
        new_meta_data = {k: v for k, v in self._meta_data.items() if v.get("data")}

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
                    if self._cache_expire:
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
                    if self._cache_expire:
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



def chinese_season_to_number(chinese_season) -> int:
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

def get_page_source(url: str, session = None, cookies = None, proxies = None, timeout: int = 5) -> str:
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

def merge_mediainfo(old_mediainfo: MediaInfo, new_mediainfo: MediaInfo) -> MediaInfo:
    '''
    合并两个媒体信息
    '''
    if not old_mediainfo.en_title:
        old_mediainfo.en_title = new_mediainfo.en_title
    if not old_mediainfo.year:
        old_mediainfo.year = new_mediainfo.year
    if not old_mediainfo.season:
        old_mediainfo.season = new_mediainfo.season
    if not old_mediainfo.original_title:
        old_mediainfo.original_title = new_mediainfo.original_title
    if not old_mediainfo.backdrop_path:
        old_mediainfo.backdrop_path = new_mediainfo.backdrop_path
    if not old_mediainfo.poster_path:
        old_mediainfo.poster_path = new_mediainfo.poster_path
    if not old_mediainfo.number_of_episodes:
        old_mediainfo.number_of_episodes = new_mediainfo.number_of_episodes
    if not old_mediainfo.number_of_seasons:
        old_mediainfo.number_of_seasons = new_mediainfo.number_of_seasons

    if not old_mediainfo.overview or (old_mediainfo.overview == '暂无简介' and new_mediainfo.overview):
        old_mediainfo.overview = new_mediainfo.overview
    if not old_mediainfo.release_date:
        old_mediainfo.release_date = new_mediainfo.release_date

    if not old_mediainfo.tagline:
        old_mediainfo.tagline = new_mediainfo.tagline
    elif new_mediainfo.tagline:
        old_mediainfo.tagline = ' '.join(list(set(old_mediainfo.tagline.split() + new_mediainfo.tagline.split())))

    if not old_mediainfo.actors:
        old_mediainfo.actors = new_mediainfo.actors
    elif new_mediainfo.actors:
        old_actor_name = [_a.get('name') for _a in old_mediainfo.actors]
        for _a in new_mediainfo.actors:
            _name = _a.get('name')
            if not _name in old_actor_name:
                old_mediainfo.actors.append(_a)

    return old_mediainfo
