import threading
import os
import re
from pathlib import Path
from time import time, sleep
from xml.dom import minidom
from typing import Optional, Any, List, Dict, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.chain.storage import StorageChain
from app.chain.media import MediaChain
from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.core.config import settings
from app.core.metainfo import MetaInfo
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MediaType, StorageSchema, NotificationType
from app.helper.directory import DirectoryHelper
from app.utils.system import SystemUtils

from .manko import MankoModule

class JAVRecognize(_PluginBase):
    # 插件名称
    plugin_name = "JAV识别"
    # 插件描述
    plugin_desc = "使用公共JAV库识别刮削"
    # 插件图标
    plugin_icon = ""
    # 插件版本
    plugin_version = "1.1"
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
    _notify = False
    _cron = None
    _scrape_dirs = []
    _media_category = None

    _recognize_srcs = {}
    _storagechain = StorageChain()

    _all_webs = {
        MankoModule.get_name(): MankoModule()    }

    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        self._event.clear()
        if config:
            self._enabled = config.get("enabled")
            self._clearcache = config.get("clearcache")
            self._onlyav = config.get("onlyav")
            self._notify = config.get("notify")
            self._cron = config.get("cron")
            self._scrape_dirs = config.get("scrape_dirs", [])
            self._media_category = config.get("media_category")

        if self.get_state():
            logger.info(f"将刮削这些目录：{self._scrape_dirs}，二级分类：{self._media_category}")
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
            "metadata_nfo": self.metadata_nfo,
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
            is_continue = False
            keywords = ["AV"]
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

    def process_missing(self, directory, missing_files, storage_type = StorageSchema.Local.value) -> bool:
        logger.info(f"目录 '{directory}' 缺失文件: {', '.join(missing_files)}")
        media_root_path = Path(directory)
        medias = SystemUtils.list_files(media_root_path, settings.RMT_MEDIAEXT)
        if not medias:
            logger.error(f"目录 {directory} 不存在媒体文件！")
            return False

        file_title = medias[0].stem
        file_meta = MetaInfo(file_title)
        file_meta.customization = "AV"
        file_meta.type = MediaType.MOVIE
        match = re.search(r'(^.*) \(\d{4}\) - (.*)$', file_meta.org_string)
        if match:
            file_meta.title = match.group(1).strip()
            file_meta.part = match.group(2).strip()

        logger.info(f"{file_meta}")

        mediainfo = self.recognize_media(file_meta)
        if not mediainfo:
            logger.warn(f"{file_title} 识别失败！")
            return False

        movie_nfo = media_root_path / "movie.nfo"
        if movie_nfo.exists():
            logger.warn(f"删除 {str(movie_nfo)}")
            movie_nfo.unlink()
        file_nfo = media_root_path / (file_title + ".nfo")
        if file_nfo.exists():
            logger.warn(f"删除 {str(file_nfo)}")
            file_nfo.unlink()

        fileitem = self._storagechain.get_file_item(storage_type, media_root_path)
        # 刮削
        MediaChain().scrape_metadata(
            fileitem=fileitem,
            mediainfo=mediainfo,
            overwrite=True
        )
        return True

    @staticmethod
    def _get_storage_type(library_path: str) -> str:
        for td in DirectoryHelper().get_library_dirs():
            if td.library_path == library_path:
                return td.storage
        return StorageSchema.Local.value

    def task_scrape(self):
        logger.info(f"开始执行JAV刮削")

        tip_message = ""
        for scrape_dir in self._scrape_dirs:
            storage_type = self._get_storage_type(scrape_dir)
            scrape_path = os.path.join(scrape_dir, self._media_category)
            logger.info(f"处理刮削路径：{scrape_path}")
            if self._event.is_set():
                logger.info("JAV刮削服务停止")
                tip_message+="JAV刮削服务停止"
                break
            for dirpath, dirnames, filenames in os.walk(scrape_path):
                if self._event.is_set():
                    logger.info("JAV刮削服务停止")
                    tip_message+="JAV刮削服务停止"
                    break
                # 如果当前目录没有子目录，则为叶子目录
                if not dirnames:
                    # 检查所需文件
                    has_poster = any(os.path.splitext(f)[0].lower() == "poster" for f in filenames)
                    has_backdrop = any(os.path.splitext(f)[0].lower() == "backdrop" for f in filenames)
                    missing = []
                    if not has_poster:
                        missing.append('poster')
                    if not has_backdrop:
                        missing.append('backdrop')
                    if missing:
                        state = self.process_missing(dirpath, missing, storage_type)
                        if state:
                            tip_message+=f"✅ {os.path.basename(dirpath)}\n"
                        else:
                            tip_message+=f"❌ {os.path.basename(dirpath)}\n"

        if self._notify and tip_message:
            self.post_message(mtype=NotificationType.Plugin,
                                title="JAV刮削任务完成",
                                text=tip_message)

        logger.info("JAV刮削任务完成")
        logger.info(f"{tip_message}")

    def scheduler_job(self):
        logger.info("定时任务...")
        for _, src in self._recognize_srcs.items():
            src.scheduler_job()

    def metadata_nfo(self, meta: MetaBase, mediainfo: MediaInfo,
                     season: Optional[int] = None, episode: Optional[int] = None) -> Optional[str]:
        need_continue = False
        # 先确认是否需要特殊处理，演员图片url是完整链接的
        if mediainfo.actors:
            for actor in mediainfo.actors:
                if actor.get("profile_path") and actor.get("profile_path").startswith("http"):
                    need_continue = True
                    break

        if not need_continue:
            return None

        if settings.SCRAP_SOURCE == "themoviedb":
            from app.modules.themoviedb.scraper import TmdbScraper
            scraper = TmdbScraper()
        elif settings.SCRAP_SOURCE == "douban":
            from app.modules.douban.scraper import DoubanScraper
            scraper = DoubanScraper()
        else:
            logger.error(f"系统使用了未配置的刮削器！")
            return None

        nfo_str = scraper.get_metadata_nfo(meta=meta, mediainfo=mediainfo)
        if nfo_str:
            return self._update_actor_in_nfo(nfo_str, mediainfo.actors)

    @staticmethod
    def _update_actor_in_nfo(nfo_str: str, new_actors: list) -> str:
        """
        更新 NFO 字符串中的演员图片链接。

        :param nfo_str: 原始 NFO 字符串
        :param actors: 演员列表
        :return: 修改后的 NFO 字符串
        """

        def remove_whitespace_text_nodes(node):
            """递归删除仅由空白字符组成的文本节点"""
            # 如果是文本节点且内容均为空白，则删除
            if node.nodeType == node.TEXT_NODE:
                if node.nodeValue and not node.nodeValue.strip():
                    node.parentNode.removeChild(node)
                return
            # 遍历子节点（注意使用 list 复制，因为循环中会修改 childNodes）
            for child in list(node.childNodes):
                remove_whitespace_text_nodes(child)

        doc = minidom.parseString(nfo_str)
        remove_whitespace_text_nodes(doc)

        # 查找所有 actor 元素
        actors = doc.getElementsByTagName('actor')
        for actor in actors:
            name_elem = actor.getElementsByTagName('name')
            if not name_elem:
                continue
            actor_name = name_elem[0].firstChild.nodeValue if name_elem[0].firstChild else ''

            for new_actor in new_actors:
                if new_actor.get("name") == actor_name:
                    if not new_actor.get("profile_path"):
                        break
                    logger.info(f"更新演员{new_actor.get("name")}缩略图{new_actor.get("profile_path")}")
                    thumb_elem = actor.getElementsByTagName('thumb')
                    if thumb_elem:
                        # 修改已有 thumb 元素的内容
                        if thumb_elem[0].firstChild:
                            thumb_elem[0].firstChild.nodeValue = new_actor.get("profile_path")
                        else:
                            # 如果 thumb 为空，创建文本节点
                            thumb_elem[0].appendChild(doc.createTextNode(new_actor.get("profile_path")))
                    else:
                        # 如果没有 thumb 元素，创建一个
                        new_thumb = doc.createElement('thumb')
                        new_thumb.appendChild(doc.createTextNode(new_actor.get("profile_path")))
                        actor.appendChild(new_thumb)
                    break

        # 返回格式化后的 XML 字符串
        return doc.toprettyxml(encoding="utf-8")

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "clearcache": self._clearcache,
            "onlyav" : self._onlyav,
            "notify": self._notify,
            "cron": self._cron,
            "scrape_dirs": self._scrape_dirs,
            "media_category": self._media_category,
        })

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state() and self._cron:
            return [
                {
                    "id": "JAVScrape",
                    "name": "JAV定时刮削",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.task_scrape,
                    "kwargs": {}
                }]

        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        library_dirs = []
        for td in DirectoryHelper().get_library_dirs():
            library_dirs.append({ "title": f"{td.name} 【{td.library_path}】", "value": td.library_path })
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyav',
                                            'label': '只识别AV',
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
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 0 0 ? *'
                                        }
                                    }
                                ]
                            }]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'clearable': True,
                                            'model': 'scrape_dirs',
                                            'label': '刮削媒体库',
                                            'items': library_dirs
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCombobox',
                                        'props': {
                                            'clearable': True,
                                            'model': 'media_category',
                                            'label': '媒体二级分类',
                                            'items': [None,"AV"],
                                            'placeholder': "留空不使用二级分类"
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
                                            'text': '只识别AV: 通过自定义识别词判断，防止对正常的媒体文件识别\n' +
                                                    '可通过其他插件（馒头资源刮削）使用，不影响本插件刮削功能',
                                            'style': 'white-space: pre-line;'
                                        }
                                    }
                                ]
                            },
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
                                            'text': '媒体库是MP配置的目录，如果MP整理开启了二级分类，请选择或输入对应的分类名\n' +
                                                    '如果你的媒体库路径是 "/media/Videos/", 二级分类是 "AV"\n' +
                                                    '那么MP整理的路径是 "/media/Videos/AV"',
                                            'style': 'white-space: pre-line;'
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
            "notify": False,
            "cron": "",
            "scrape_dirs": [],
            "media_category": ""
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
