import re
import requests


from typing import Optional, Tuple, Union

from lxml import etree
from xpinyin import Pinyin

from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.modules import _ModuleBase
from app.modules.themoviedb import TheMovieDbModule
from app.log import logger
from app.schemas.types import MediaType, ModuleType, MediaRecognizeType
from app.utils.http import RequestUtils

from .myutils import chinese_season_to_number
from .myutils import get_page_source
from .myutils import meta_update
from .myutils import PlayletCache, PlayletScraper


class TMDBModule(_ModuleBase):

    '''
    TMDB媒体信息匹配
    '''
    # 元数据缓存
    cache: PlayletCache = None
    # 红果免费短剧
    tmdb: TheMovieDbModule = None
    # 刮削器
    scraper: PlayletScraper = None

    def init_module(self) -> None:
        self.tmdb = TheMovieDbModule()
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
        # 网页版不支持特殊字符搜索，删除特殊字符
        search_name = re.sub('[ ，,：:]', '', meta.cn_name)

        if cache:
            # 读取缓存
            cache_info = self.cache.get(meta)
            if cache_info:
                cache_data = cache_info.get('data')
                if cache_data.title == search_name:
                    mediainfos = [cache_data]

        if not mediainfos:
            mediainfos = self.hongguo.search(search_name)

        if not mediainfos:
            return None

        logger.debug(f"mediainfos={mediainfos}")

        if len(mediainfos) == 0:
            return None

        _mediainfo: MediaInfo = mediainfos[0]
        if len(mediainfos) > 1:
            for _m in mediainfos:
                if _m.title == search_name:
                    _mediainfo = _m
                    break

        self.cache.update(meta, _mediainfo)

        return _mediainfo

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        self.cache.save()
