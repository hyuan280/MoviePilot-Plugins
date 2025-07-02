import importlib
from typing import Optional, Any, List, Dict, Tuple

from app.helper.sites import SitesHelper
from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.modules.themoviedb import TheMovieDbModule
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType

from .hongguomodule import HongGuoModule
from .sitemodule import SiteModule
from .myutils import merge_mediainfo


class PlayletAutoRecognize(_PluginBase):
    # 插件名称
    plugin_name = "短剧自动识别"
    # 插件描述
    plugin_desc = "从TMDB、网站和站点识别短剧"
    # 插件图标
    plugin_icon = "Amule_B.png"
    # 插件版本
    plugin_version = "1.4.2"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "playlet_"
    # 加载顺序
    plugin_order = 20
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _clearcache = False
    _onlyplaylet = True
    _playlet_keywords = ""
    _searchwebs = []
    _searchsites = []
    _torrent_dirs = ""

    _site_infos = []
    _recognize_srcs = {}

    _all_webs = {
        TheMovieDbModule.get_name(): TheMovieDbModule(),
        HongGuoModule.get_name(): HongGuoModule()
    }

    def init_plugin(self, config: dict = None):
        self._site_infos = []
        self._recognize_srcs = {}

        if config:
            self._enabled = config.get("enabled")
            self._clearcache = config.get("clearcache")
            self._onlyplaylet = config.get("onlyplaylet")
            self._playlet_keywords = config.get("playlet_keywords")
            self._searchwebs = config.get("searchwebs", [])
            self._searchsites = config.get("searchsites", [])
            self._torrent_dirs = config.get("torrent_dirs")

        logger.info(f"插件使能：{self._enabled}")

        if self._searchsites:
            site_id_to_public_status = {site.get("id"): site.get("public") for site in SitesHelper().get_indexers()}
            self._searchsites = [
                site_id for site_id in self._searchsites
                if site_id in site_id_to_public_status and not site_id_to_public_status[site_id]
            ]

        if not self._enabled:
            return

        for siteid in self._searchsites:
            siteinfo = SiteOper().get(siteid)
            if siteinfo:
                self._site_infos.append(siteinfo)
        if self._site_infos:
            logger.info(f"即将从站点 {', '.join(site.name for site in self._site_infos)} 查找媒体信息")

        if self._searchwebs:
            for _module_name in self._searchwebs:
                _module = self._all_webs.get(_module_name)
                if _module:
                    _module.init_module()
                    if not _module.test():
                        logger.error(f"{_module_name}网络连接失败")
                        continue
                    self._recognize_srcs[_module_name] = _module

        if self._searchsites:
            if not self._recognize_srcs.get(SiteModule.get_name()):
                _module = SiteModule(self._searchsites, self._torrent_dirs.split('\n') if self._torrent_dirs else [])
                _module.init_module()
                self._recognize_srcs[SiteModule.get_name()] = _module

        if self._clearcache:
            for _,src in self._recognize_srcs.items():
                if src:
                    src.clear_cache()
            self._clearcache = False

        # 更新配置
        self.__update_config()

    def get_module(self) -> Dict[str, Any]:
        if not self._enabled:
            return None
        return {
            "recognize_media": self.recognize_media,
            "obtain_images": self.obtain_images,
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

    @staticmethod
    def __import_meta_search_tv_name():
        def meta_search_tv_name(file_meta: MetaBase, tv_name: str, is_compilations: bool = False):
            return file_meta
        try:
            module = importlib.import_module("app.plugins.playletpolishscrape")
            import_func = getattr(module, "meta_search_tv_name")
        except (ModuleNotFoundError, AttributeError):
            import_func = meta_search_tv_name
        return import_func

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
            return None
        if not meta.name:
            logger.error("识别媒体信息时未提供元数据名称")
            return None

        if mtype:
            meta.type = mtype

        if meta.isfile and meta.type != MediaType.TV:
            logger.error("短剧只识别电视剧")
            return None

        logger.debug(f"{meta}")

        if self._onlyplaylet:
            if not meta.customization:
                logger.debug(f"{meta.name} 没有识别词，跳过")
                return None
            if not self._playlet_keywords:
                logger.debug(f"{meta.name} 没有短剧关键词，跳过")
                return None
            is_continue = False
            keywords = self._playlet_keywords.split("\n")
            keywords.append("短剧")
            for keyword in keywords:
                if keyword in meta.customization.split('@'):
                    is_continue = True
                    break
            if not is_continue:
                logger.debug(f"{meta.name} 识别词不是短剧关键词，跳过")
                return None
        else:
            if not self._onlyplaylet:
                func = self.__import_meta_search_tv_name()
                if func:
                    meta = func(meta, meta.cn_name)

        if not meta.cn_name:
            logger.warn(f"{meta.name} 红果短剧只支持中文标题搜索")
            return None

        result_sum = None
        for src_name,src in self._recognize_srcs.items():
            logger.debug(f"{src_name} 开始搜索...")
            try:
                result = src.recognize_media(meta=meta, mtype=mtype, tmdbid=tmdbid, doubanid=doubanid, bangumiid=bangumiid, episode_group=episode_group, cache=cache)
            except Exception as e:
                logger.error(f"{src_name} 识别出错: {e}")
            if result:
                if not result_sum:
                    result_sum = result
                else:
                    result_sum = merge_mediainfo(result_sum, result)

        if result_sum and self._onlyplaylet:
            result_sum.category = '短剧'

        logger.debug(f"result_sum:{result_sum}")
        return result_sum

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyplaylet" : self._onlyplaylet,
            "playlet_keywords": self._playlet_keywords,
            "searchwebs": self._searchwebs,
            "searchsites": self._searchsites,
            "clearcache": self._clearcache,
            "torrent_dirs": self._torrent_dirs,
        })

    def scheduler_job(self):
        logger.info("定时任务...")
        for _, src in self._recognize_srcs.items():
            src.scheduler_job()

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        site_options = [{"title": site.get("name"), "value": site.get("id")}
                        for site in SitesHelper().get_indexers()]
        web_options = list(self._all_webs.keys())
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
                                            'model': 'clearcache',
                                            'label': '清空缓存',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyplaylet',
                                            'label': '只识别短剧，通过短剧关键词判断',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'playlet_keywords',
                                            'label': '短剧关键词',
                                            'rows': 2,
                                            'placeholder': '一行一个'
                                        }
                                    }
                                ]
                            }
                        ]
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'searchwebs',
                                            'label': '刮削网站',
                                            'items': web_options
                                        }
                                    }
                                ]
                            }
                        ]
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
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'searchsites',
                                            'label': '刮削站点',
                                            'items': site_options
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'torrent_dirs',
                                            'label': '种子文件目录',
                                            'rows': 2,
                                            'placeholder': '每一行一个目录'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '短剧关键词 配置说明: 在mp设置自定义占位符，然后这里配置同样的词，可以是你短剧的保存目录名，需要注意mp只识别文件的上两层目录，短剧路径过深会识别不到'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '整理默认二级分类是“短剧”，开始只识别短剧，所有识别的电视剧都会被分类为“短剧”'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "clearcache": False,
            "onlyplaylet": True,
            "playlet_keywords": "",
            "searchwebs": [],
            "searchsites": [],
            "torrent_dirs": "",
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
        for _, src in self._recognize_srcs.items():
            src.stop()
