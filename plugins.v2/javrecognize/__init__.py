import threading
from time import time, sleep
from typing import Optional, Any, List, Dict, Tuple

from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType

from .manko import MankoModule

class JAVRecognize(_PluginBase):
    # 插件名称
    plugin_name = "JAV识别"
    # 插件描述
    plugin_desc = "使用公共JAV库识别刮削"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.0"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "javrecognize_"
    # 加载顺序
    plugin_order = 25
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _clearcache = False
    _onlyav = True

    _recognize_srcs = {}

    _all_webs = {
        MankoModule.get_name(): MankoModule()    }

    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._clearcache = config.get("clearcache")
            self._onlyav = config.get("onlyav")

        logger.info(f"插件使能：{self._enabled}")

        if self.get_state():
            for _module_name, _module in self._all_webs.items():
                _module.init_module()
                if not _module.test():
                    logger.error(f"{_module_name}网络连接失败")
                    continue
                self._recognize_srcs[_module_name] = _module

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
            "async_recognize_media": self.async_recognize_media,
            "scheduler_job": self.scheduler_job,
        }

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

        if meta.isfile and meta.type != MediaType.MOVIE:
            logger.error("AV只识别电影")
            return None

        logger.debug(f"{meta}")

        if self._onlyav:
            if not meta.customization:
                logger.debug(f"{meta.name} 没有识别词，跳过")
                return None
            keywords = []
            is_continue = False
            keywords.append("AV")
            for keyword in keywords:
                if keyword in meta.customization.split('@'):
                    is_continue = True
                    break
            if not is_continue:
                logger.debug(f"{meta.name} 识别词不是AV关键词，跳过")
                return None

        result = None
        for src_name,src in self._recognize_srcs.items():
            logger.debug(f"{src_name} 开始搜索...")
            try:
                result = src.recognize_media(meta=meta, mtype=mtype, tmdbid=tmdbid, doubanid=doubanid, bangumiid=bangumiid, episode_group=episode_group, cache=cache)
            except Exception as e:
                logger.error(f"{src_name} 识别出错: {e}")

            if result:
                break

        if result and self._onlyav:
            result.category = 'AV'

        logger.debug(f"result:{result}")
        return result

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
        if not meta.name:
            logger.error("识别媒体信息时未提供元数据名称")
            return None

        if mtype:
            meta.type = mtype

        if meta.isfile and meta.type != MediaType.MOVIE:
            logger.error("AV只识别电影")
            return None

        logger.debug(f"{meta}")

        if self._onlyav:
            if not meta.customization:
                logger.debug(f"{meta.name} 没有识别词，跳过")
                return None
            keywords = []
            is_continue = False
            keywords.append("AV")
            for keyword in keywords:
                if keyword in meta.customization.split('@'):
                    is_continue = True
                    break
            if not is_continue:
                logger.debug(f"{meta.name} 识别词不是AV关键词，跳过")
                return None

        result = None
        for src_name,src in self._recognize_srcs.items():
            logger.debug(f"{src_name} 开始搜索...")
            try:
                result = await src.async_recognize_media(meta=meta, mtype=mtype, tmdbid=tmdbid, doubanid=doubanid, bangumiid=bangumiid, episode_group=episode_group, cache=cache)
            except Exception as e:
                logger.error(f"{src_name} 识别出错: {e}")

            if result:
                break

        if result and self._onlyav:
            result.category = 'AV'

        logger.debug(f"result:{result}")
        return result

    def scheduler_job(self):
        logger.info("定时任务...")
        for _, src in self._recognize_srcs.items():
            src.scheduler_job()

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "onlyav" : self._onlyav,
            "clearcache": self._clearcache,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
                                            'model': 'onlyav',
                                            'label': '只识别AV，只能通过插件调用',
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
            "onlyav": True,
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        self._event.set()
        for _, src in self._recognize_srcs.items():
            src.stop()
        self._event.clear()

