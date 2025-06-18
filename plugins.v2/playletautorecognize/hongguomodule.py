import re
import requests


from typing import Optional, Tuple, Union

from lxml import etree
from xpinyin import Pinyin

from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.modules import _ModuleBase
from app.log import logger
from app.schemas.types import MediaType, ModuleType, MediaRecognizeType
from app.utils.http import RequestUtils

from .myutils import chinese_season_to_number
from .myutils import get_page_source
from .myutils import meta_search_tv_name
from .myutils import PlayletCache, PlayletScraper
#
# 红果免费短剧 识别api（网页版）
#
class HongGuoApi():
    _base_url = "https://www.hgdj.app"
    _search_url = ""
    _session = None

    def __init__(self):
        self._search_url = self.__get_search_url()
        self._session = requests.Session()

    def __get_search_url(self):
        """
        从首页获取搜索url
        """
        return "https://www.hgdj.app/vodsearch/-------------.html"
        page_source = get_page_source(self._base_url, self._session)

        if not page_source:
            logger.error("网站没有请求到数据")
            return None

        html = etree.HTML(page_source)
        if not html:
            return None

        search_action = html.xpath('//div[@class="conch-search"]//form[contains(@class, "hl-search-selop")]/@action')
        if not search_action:
            logger.error("没找到搜索网址")
            return None

        return f"{self._base_url}{search_action[0]}"

    def __get_mediainfo(self, detail_url):
        page_source = get_page_source(detail_url, self._session)
        if not page_source:
            logger.warn(f"请求无效数据：{detail_url}")
            return None

        mediainfo = MediaInfo()
        mediainfo.source = 'hongguo'
        mediainfo.type = MediaType.TV

        result_dict = {}

        html = etree.HTML(page_source)
        if not html:
            return None

        # 获取媒体信息
        img_elements = html.xpath('//div[@class="conch-content"]//div[contains(@class, "hl-dc-pic")]//span[contains(@class, "hl-item-thumb")]/@data-original')
        if img_elements:
            mediainfo.poster_path = f"{self._base_url}{img_elements[0]}"
            mediainfo.backdrop_path = mediainfo.poster_path
        li_elements = html.xpath('//div[@class="hl-dc-content"]//div[contains(@class, "hl-full-box")]/ul//li')
        if li_elements:
            for li_element in li_elements:
                em_text = li_element.xpath('./em')[0].text
                li_text = li_element.xpath('string(.)')

                result_dict[em_text.replace('：', '').strip()] = li_text.replace(em_text, '').strip()

        logger.info(f"result_dict={result_dict}")

        tags = []
        for k,v in result_dict.items():
            if k == '片名':
                mediainfo.original_title = v
                match = re.match(r'([^\d]+)(第.*?季)', v)
                if match:
                    mediainfo.title = match.group(1).strip()
                    mediainfo.season = chinese_season_to_number(match.group(2))
                else:
                    mediainfo.title = v
                    mediainfo.season = 1
            elif k == '年份':
                mediainfo.year = v
            elif k == '简介':
                mediainfo.overview = v
            elif k == '上映':
                if v != '未知':
                    mediainfo.release_date = v
            elif k == '更新':
                if not mediainfo.release_date:
                    mediainfo.release_date = v
            elif k == '类型':
                if v != '未知':
                    tags.append(v)
            elif k == '语言':
                if v != '未知':
                    mediainfo.original_language = v
            elif k == '导演':
                if v != '未知':
                    directors = v.split('/')
                    mediainfo.directors = [_s.strip() for _s in directors]
            elif k == '主演':
                if v != '未知':
                    actors = v.split('/')
                    mediainfo.actors = [_s.strip() for _s in actors]

        mediainfo.tagline = ' '.join(tags) if tags else None
        mediainfo.mediaid_prefix = 'hongguo'
        mediainfo.category = "短剧"
        logger.info(f"mediainfo={mediainfo}")
        return mediainfo

    def search(self, meta):
        mediainfos = []
        logger.info(f"红果短剧搜索：{meta.cn_name} ...")
        page_source = get_page_source(f"{self._search_url}?wd={meta.cn_name}&submit=", self._session)
        if not page_source:
            logger.warn("网站没有请求到数据")
            return None

        html = etree.HTML(page_source)
        if not html:
            return None

        search_items = html.xpath('//div[@class="hl-item-content"]//a[@class="hl-btn-border"]/@href')
        if not search_items:
            logger.info(f"红果短剧没有 {meta.cn_name}")
            return None

        for item in search_items:
            detail_url = f"{self._base_url}{item}"
            _mediainfo = self.__get_mediainfo(detail_url)
            if _mediainfo:
                mediainfos.append(_mediainfo)
        logger.info("解析完成")
        return mediainfos

    def test(self):
        ret = RequestUtils().get_res(self._base_url)
        if ret is None:
            return False
        return True

    def close(self):
        if self._session:
            self._session.close()

class HongGuoModule(_ModuleBase):

    '''
    红果免费短剧媒体信息匹配
    '''
    # 元数据缓存
    cache: PlayletCache = None
    # 红果免费短剧
    hongguo: HongGuoApi = None
    # 刮削器
    scraper: PlayletScraper = None

    def init_module(self) -> None:
        self.hongguo = HongGuoApi()
        self.scraper = PlayletScraper()
        self.cache = PlayletCache()

    def stop(self):
        self.cache.save()
        self.hongguo.close()

    def test(self) -> Tuple[bool, str]:
        if self.hongguo.test():
            return False, "红果网络连接失败"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def get_name() -> str:
        return "红果"

    @staticmethod
    def get_type() -> ModuleType:
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        return MediaRecognizeType.TMDB

    @staticmethod
    def get_priority() -> int:
        return 0



    def recognize_media(self, meta: MetaBase = None,
                        mtype: Optional[MediaType] = None,
                        tmdbid: Optional[int] = None,
                        doubanid: Optional[str] = None,
                        bangumiid: Optional[int] = None,
                        episode_group: Optional[str] = None,
                        cache: bool = True) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param doubanid: 豆瓣ID
        :param bangumiid: BangumiID
        :param episode_group: 剧集组
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """
        mediainfos = []
        if not meta:
            return None
        if not meta.name:
            logger.error("识别媒体信息时未提供元数据名称")
            return None

        if mtype:
            meta.type = mtype

        if meta.isfile and meta.type != MediaType.TV:
            logger.error("短剧短剧只识别电视剧")
            return None

        if meta.cn_name:
            meta = meta_search_tv_name(meta, meta.cn_name)

        # 网页版不支持特殊字符搜索，删除特殊字符
        meta.cn_name = re.sub('[ ，,：:]', '', meta.cn_name)

        if cache:
            # 读取缓存
            cache_info = self.cache.get(meta)
            if cache_info:
                cache_data = cache_info.get('data')
                if cache_data.title == meta.cn_name:
                    mediainfos = [cache_data]

        logger.info(f"1--{mediainfos}")

        if not mediainfos:
            mediainfos = self.hongguo.search(meta)

        if not mediainfos:
            return None

        logger.info(f"2--{mediainfos}")

        if len(mediainfos) == 0:
            return None

        _mediainfo: MediaInfo = mediainfos[0]
        if len(mediainfos) > 1:
            for _m in mediainfos:
                if _m.title == meta.cn_name:
                    _mediainfo = _m
                    break

        self.cache.update(meta, _mediainfo)

        return _mediainfo

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        self.cache.save()
