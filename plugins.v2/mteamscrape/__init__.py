import importlib
import re
from typing import Optional, Any, List, Dict, Tuple

from app.helper.sites import SitesHelper
from app.core.config import settings
from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.modules.themoviedb import TheMovieDbModule
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType

from app.utils.http import RequestUtils, AsyncRequestUtils

class MTeamScrape(_PluginBase):
    # 插件名称
    plugin_name = "馒头资源刮削"
    # 插件描述
    plugin_desc = "从馒头站点识别资源刮削"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/hyuan280/MoviePilot-Plugins/main/icons/MTeam.png"
    # 插件版本
    plugin_version = "1.0.2"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "mteamscrape_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 2

    # 站点属性
    _search_url = "https://api.m-team.cc/api/torrent/search"
    _movie_category = ['401', '419', '420', '421', '439', '405', '404']
    _tv_category = ['403', '402', '435', '438', '404', '405']

    _av_category = ["410", "424", "437", "431", "429", "430", "426", "432", "436", "440", ]
    _iv_category = ["425", "433"]
    _h_category = {"411": "H-遊戲", "412": "H-動畫", "413": "H-漫畫"}

    # 私有属性
    _enabled = False
    _site_info = None
    _resource_regulars = ""


    _res_regs = []

    def init_plugin(self, config: dict = None):
        self._res_regs = []

        if config:
            self._enabled = config.get("enabled", False)
            self._resource_regulars = config.get("resource_regulars", "")

        if self._enabled:
            for site in SitesHelper().get_indexers():
                if site.get("name") == "馒头":
                    self._site_info = site

        if self._site_info is None:
            self._enabled = False

        if self._enabled:
            if self._resource_regulars != "":
                resource_regulars = self._resource_regulars.split("\n")
                if len(resource_regulars) > 0:
                    for res_reg in resource_regulars:
                        res_regs = res_reg.split("::")
                        if len(res_regs) != 2 and len(res_regs) != 4 and len(res_regs) != 6:
                            logger.error(f"配置格式错误：{res_reg}, 配置方式(mode::reg[::group::text][::group::text])")
                            self._enabled = False
                            break

                        mode = res_regs[0]
                        reg = res_regs[1]
                        if len(res_regs) >= 4:
                            try:
                                group1 = int(res_regs[2])
                                text1 = res_regs[3]
                            except Exception as e:
                                logger.error(f"配置捕获组错误：{res_reg}, 配置方式(mode::reg::group::text[::group::text])")
                                self._enabled = False
                                break
                        else:
                            group1 = 0
                            text1 = "%s"
                        if len(res_regs) >= 6:
                            try:
                                group2 = int(res_regs[4])
                                text2 = res_regs[5]
                            except Exception as e:
                                logger.error(f"配置捕获组错误：{res_reg}, 配置方式(mode::reg::group::text::group::text)")
                                self._enabled = False
                                break
                        else:
                            group2 = 0
                            text2 = "%s"
                        self._res_regs.append({"mode": mode, "reg": reg, "name_group": group1, "name_text": text1, "search_group": group2, "search_text": text2})

            logger.info(f"插件使能：{self._enabled}")
            logger.info(f"您的配置：{self._res_regs}")
        # 更新配置
        self.__update_config()

    def get_module(self) -> Dict[str, Any]:
        if not self._enabled:
            return None
        return {
            "recognize_media": self.recognize_media,
            "async_recognize_media": self.async_recognize_media,
            #"obtain_images": self.obtain_images,
            "scheduler_job": self.scheduler_job,
        }

    def obtain_images(self, mediainfo: MediaInfo) -> Optional[MediaInfo]:
        """
        补充抓取媒体信息图片
        :param mediainfo:  识别的媒体信息
        :return: 更新后的媒体信息
        """
        if mediainfo.category == '短剧':
            return mediainfo
        return None

    def __get_params(self, mode: str, keyword: str, mtype: MediaType = None, page: Optional[int] = 0) -> dict:
        """
        获取请求参数
        """
        if not mtype:
            categories = []
        elif mtype == MediaType.TV:
            categories = self._tv_category
        else:
            categories = self._movie_category
        return {
            "mode": mode,
            "keyword": keyword,
            "categories": categories,
            "pageNumber": int(page) + 1,
            "pageSize": 100,
            "visible": 1
        }

    def search(self, mode: str, keyword: str, mtype: MediaType = None, page: Optional[int] = 0):
        """
        搜索
        """
        site = SiteOper().get(self._site_info.get("id"))
        # 检查ApiKey
        if not site.apikey:
            return []

        _proxy = None
        if site.proxy:
            _proxy = settings.PROXY

        ua = site.ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
        params = self.__get_params(mode, keyword, mtype, page)

        # 发送请求
        res = RequestUtils(
            headers={
                "Content-Type": "application/json",
                "User-Agent": ua,
                "x-api-key": site.apikey,
            },
            proxies=_proxy,
            referer=f"{site.domain}browse",
            timeout=site.timeout or 60
        ).post_res(url=self._search_url, json=params)
        if res and res.status_code == 200:
            results = res.json().get('data', {}).get("data") or []
            return results
        elif res is not None:
            logger.warn(f"搜索 {keyword} 失败，错误码：{res.status_code}")
            return []
        else:
            logger.warn(f"搜索 {keyword} 失败，无法连接 {self._domain}")
            return []

    async def async_search(self, mode: str, keyword: str, mtype: MediaType = None, page: Optional[int] = 0):
        """
        搜索
        """
        site = SiteOper().get(self._site_info.get("id"))
        # 检查ApiKey
        if not site.apikey:
            return []

        _proxy = None
        if site.proxy:
            _proxy = settings.PROXY

        ua = site.ua or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
        params = self.__get_params(mode, keyword, mtype, page)

        # 发送请求
        res = await AsyncRequestUtils(
            headers={
                "Content-Type": "application/json",
                "User-Agent": ua,
                "x-api-key": site.apikey,
            },
            proxies=_proxy,
            referer=f"{site.domain}/browse",
            timeout=site.timeout or 60
        ).post_res(url=self._search_url, json=params)
        if res and res.status_code == 200:
            results = res.json().get('data', {}).get("data") or []
            return results
        elif res is not None:
            logger.warn(f"搜索 {keyword} 失败，错误码：{res.status_code}")
            return []
        else:
            logger.warn(f"搜索 {keyword} 失败，无法连接 {self._domain}")
            return []

    def __parse_mediainfo(self, meta: MetaBase, data: dict):

        mediainfo = MediaInfo()
        mediainfo.source = 'mteam'
        mediainfo.type = MediaType.MOVIE

        # 获取媒体信息
        mediainfo.original_title = data.get("name")
        for res_reg in self._res_regs:
            reg = res_reg.get("reg")
            group = res_reg.get("name_group")
            text = res_reg.get("name_text")
            r = re.search(reg, mediainfo.original_title)
            if r:
                try:
                    mediainfo.title = text % r.group(group).strip()
                except IndexError as e:
                    logger.error(f"re匹配没有组{group}")
                    mediainfo.title = r.group(0).strip()
                mediainfo.en_title = mediainfo.title
                break

        if not mediainfo.title:
            logger.error(f"没有匹配标题：{data}")
            return None

        imageList = data.get("imageList")
        if imageList:
            mediainfo.poster_path = imageList[0]
            mediainfo.backdrop_path = mediainfo.poster_path

        dmmInfo = data.get("dmmInfo")
        if dmmInfo:
            mediainfo.directors = [{ "name": dmmInfo.get("maker") }]
            mediainfo.actors = [{ "name": dmmInfo.get("director"), 'type': 'Director' }]
            tags = [dmmInfo.get("label")] + dmmInfo.get("keywordList")
            mediainfo.tagline = ' '.join(tags) if tags else None

        createdDate = data.get("createdDate")
        if createdDate:
            if meta.year:
                mediainfo.year = meta.year
            else:
                mediainfo.year = createdDate.split("-")[0]
            mediainfo.release_date = createdDate

        smallDescr = data.get("smallDescr")
        if not smallDescr:
            mediainfo.overview = mediainfo.original_title
        else:
            name_ex = mediainfo.original_title.replace(mediainfo.title, "").strip()
            if len(name_ex) > 5 and re.search(r'[^a-zA-Z0-9\-\s\.]', name_ex):
                mediainfo.overview = smallDescr + " " + name_ex
            else:
                mediainfo.overview = smallDescr

        category = data.get("category")
        if category in self._av_category:
            mediainfo.category = "AV"
        elif category in self._iv_category:
            mediainfo.category = "AV"
        elif self._h_category.get(category):
            mediainfo.category = self._h_category.get(category)

        return mediainfo

    def __parse_res_match(self, res_name: str, meta: MetaBase, results: List[dict]):
        """
        解析搜索结果
        """
        mediainfos = []
        if not results:
            return mediainfos

        for result in results:
            mediainfo = self.__parse_mediainfo(meta, result)
            if mediainfo:
                mediainfos.append(mediainfo)

        return mediainfos

    def __name_match(self, res_name):
        reg_matchs = []
        res_names = []
        for res_reg in self._res_regs:
            reg = res_reg.get("reg")
            group = res_reg.get("search_group")
            text = res_reg.get("search_text")
            r = re.search(reg, res_name)
            if r:
                _res_name = text % r.group(group)
                if _res_name not in res_names:
                    res_names.append(_res_name)
                    reg_match = {"mode": res_reg.get("mode"), "res_name": _res_name}
                    part_reg = f"{reg}-(\\d+)\\b"
                    p = re.search(part_reg, res_name)
                    if p:
                        part_str = p.groups()[-1]
                        reg_match["part"] = part_str
                    reg_matchs.append(reg_match)

        return reg_matchs

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
        if not meta:
            logger.error("空的meta")
            return None
        logger.info(f"使用馒头识别 meta={meta}")
        if not meta.org_string:
            logger.error("识别媒体信息时未提供元数据文件名")
            return None

        res_name = meta.org_string
        logger.info(f"使用馒头识别 {res_name}")

        reg_matchs = self.__name_match(res_name)

        logger.debug(f"将搜索下列资源：{reg_matchs}")

        for reg_match in reg_matchs:
            result = self.search(reg_match.get("mode"), reg_match.get("res_name"))
            res_match = self.__parse_res_match(res_name, meta, result)
            if res_match:
                reg_match["mediainfo"] = res_match

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            for mediainfo in mediainfos:
                if mediainfo.poster_path and mediainfo.title == reg_match.get("res_name"):
                    if reg_match.get("part"):
                        meta.part=reg_match.get("part")
                    return mediainfo

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            for mediainfo in mediainfos:
                if mediainfo.title == reg_match.get("res_name"):
                    if reg_match.get("part"):
                        meta.part=reg_match.get("part")
                    return mediainfo

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            for mediainfo in mediainfos:
                if mediainfo.poster_path:
                    if reg_match.get("part"):
                        meta.part=reg_match.get("part")
                    return mediainfo

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            if reg_match.get("part"):
                meta.part=reg_match.get("part")
            return mediainfos[0]

        return None

    async def async_recognize_media(self, meta: MetaBase = None,
                        mtype: Optional[MediaType] = None,
                        tmdbid: Optional[int] = None,
                        doubanid: Optional[str] = None,
                        bangumiid: Optional[int] = None,
                        episode_group: Optional[str] = None,
                        cache: bool = True) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片(异步版本)
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param doubanid: 豆瓣ID
        :param bangumiid: BangumiID
        :param episode_group: 剧集组
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """

        if not meta:
            return None
        if not meta.org_string:
            logger.error("识别媒体信息时未提供元数据文件名")
            return None

        res_name = meta.org_string
        logger.info(f"使用馒头识别 {res_name}")

        reg_matchs = self.__name_match(res_name)

        logger.debug(f"将搜索下列资源：{reg_matchs}")

        mediainfos = []
        for reg_match in reg_matchs:
            result = await self.async_search(reg_match.get("mode"), reg_match.get("res_name"))
            res_match = self.__parse_res_match(res_name, meta, result)
            if res_match:
                reg_match["mediainfo"] = res_match

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            for mediainfo in mediainfos:
                if mediainfo.poster_path and mediainfo.title == reg_match.get("res_name"):
                    if reg_match.get("part"):
                        meta.part=reg_match.get("part")
                    return mediainfo

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            for mediainfo in mediainfos:
                if mediainfo.title == reg_match.get("res_name"):
                    if reg_match.get("part"):
                        meta.part=reg_match.get("part")
                    return mediainfo

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            for mediainfo in mediainfos:
                if mediainfo.poster_path:
                    if reg_match.get("part"):
                        meta.part=reg_match.get("part")
                    return mediainfo

        for reg_match in reg_matchs:
            mediainfos = reg_match.get("mediainfo")
            if not mediainfos:
                continue
            if reg_match.get("part"):
                meta.part=reg_match.get("part")
            return mediainfos[0]

        return None

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "resource_regulars": self._resource_regulars,
        })

    def scheduler_job(self):
        logger.info("定时任务...")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                        ],
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'resource_regulars',
                                            'label': '资源匹配正则表达式',
                                            'rows': 3,
                                            'placeholder': '''模式::名称正则表达式::刮削名称捕获组::刮削名称格式化字符串::搜索名称捕获组::搜索名称格式化字符串
adult::[a-zA-Z]+-[0-9]+::0::%s::0::%s
adult::^(FC2-?PPV-)([0-9]{7,})::2::FC2-PPV-%s::2::PPV-%s'''
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ],
            }
        ], {
            "enabled": False,
            "resource_regulars": "",
        }

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        停止服务
        """
        pass

