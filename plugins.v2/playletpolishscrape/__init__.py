import datetime
from ntpath import isfile
import os
import re
import threading
from pathlib import Path
from threading import Lock
from typing import Any, List, Dict, Tuple, Optional
from xml.dom import minidom
from xpinyin import Pinyin

import chardet
import pytz
from PIL import Image
from apscheduler.schedulers.background import BackgroundScheduler
from lxml import etree
from requests import RequestException
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from app.helper.sites import SitesHelper

from app.chain.search import SearchChain
from app.chain.storage import StorageChain
from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.core.context import MediaInfo
from app.db.site_oper import SiteOper
from app.helper.directory import DirectoryHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import TransferInfo, TransferDirectoryConf
from app.schemas.types import NotificationType, MediaType, StorageSchema
from app.utils.common import retry
from app.utils.dom import DomUtils
from app.utils.http import RequestUtils
from app.utils.system import SystemUtils
from app.utils.string import StringUtils

ffmpeg_lock = threading.Lock()
lock = Lock()


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, watching_path: str, file_change: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._watch_path = watching_path
        self.file_change = file_change

    def on_created(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.src_path)

    def on_moved(self, event):
        self.file_change.event_handler(event=event, source_dir=self._watch_path, event_path=event.dest_path)


class PlayletPolishScrape(_PluginBase):
    # 插件名称
    plugin_name = "短剧整理刮削"
    # 插件描述
    plugin_desc = "监控目录，整理短剧，刮削短剧"
    # 插件图标
    plugin_icon = "Amule_B.png"
    # 插件版本
    plugin_version = "1.4.2"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "platletps_"
    # 加载顺序
    plugin_order = 26
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _monitor_confs = None
    _rename_title = None
    _onlyonce = False
    _link_pass = False
    _exclude_keywords = ""
    _polish_keywords = ""
    _transfer_type = "link"
    _storage_type = StorageSchema.Local.value
    _observer = []
    _timeline = "00:00:10"
    _dirconf = {}
    _coverconf = {}
    _interval = 10
    _notify = False
    _medias = {}
    _searchsites = []
    _site_infos = []
    _site_cache = {}
    _error_count = 5
    _img_error_cache = {}
    _search_error_cache = {}
    _name_error_cache = {}

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._coverconf = {}
        self._site_infos = []
        self._site_cache = {}
        self._img_error_cache = {}
        self._search_error_cache = {}
        self._name_error_cache = {}

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._link_pass = config.get("link_pass")
            self._searchsites = config.get("searchsites", [])
            self._interval = config.get("interval")
            self._notify = config.get("notify")
            self._monitor_confs = config.get("monitor_confs")
            self._rename_title = config.get("rename_title")
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._polish_keywords = config.get("polish_keywords") or ""
            self._transfer_type = config.get("transfer_type") or "link"
            self._storage_type = config.get("storage_type") or StorageSchema.Local.value

        # 停止现有任务
        self.stop_service()

        if self._searchsites:
            site_id_to_public_status = {site.get("id"): site.get("public") for site in SitesHelper().get_indexers()}
            self._searchsites = [
                site_id for site_id in self._searchsites
                if site_id in site_id_to_public_status and not site_id_to_public_status[site_id]
            ]

        self.__update_config()

        # 获取所有站点的信息，并过滤掉不存在的站点
        for siteid in self._searchsites:
            siteinfo = SiteOper().get(siteid)
            if siteinfo:
                self._site_infos.append(siteinfo)
        if self._site_infos:
            logger.info(f"即将从站点 {', '.join(site.name for site in self._site_infos)} 查找媒体信息")

        if self._enabled or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            if self._notify:
                # 追加入库消息统一发送服务
                self._scheduler.add_job(self.send_msg, trigger='interval', seconds=15)

            # 读取目录配置
            monitor_confs = self._monitor_confs.split("\n")
            if not monitor_confs:
                return
            for monitor_conf in monitor_confs:
                # 格式 监控方式#监控目录#目的目录#是否重命名#封面比例
                if not monitor_conf:
                    continue
                if str(monitor_conf).count("#") != 2:
                    logger.error(f"{monitor_conf} 格式错误")
                    continue
                mode = str(monitor_conf).split("#")[0]
                source_dir = str(monitor_conf).split("#")[1]
                target_dir = str(monitor_conf).split("#")[2]

                # 存储目录监控配置
                self._dirconf[source_dir] = target_dir

                # 启用目录监控
                if self._enabled:
                    # 检查媒体库目录是不是下载目录的子目录
                    try:
                        if target_dir and Path(target_dir).is_relative_to(Path(source_dir)):
                            logger.warn(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            self.systemmessage.put(f"{target_dir} 是下载目录 {source_dir} 的子目录，无法监控")
                            continue
                    except Exception as e:
                        logger.debug(str(e))
                        pass

                    try:
                        if mode == "compatibility":
                            # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                            observer = PollingObserver(timeout=10)
                        else:
                            # 内部处理系统操作类型选择最优解
                            observer = Observer(timeout=10)
                        self._observer.append(observer)
                        observer.schedule(FileMonitorHandler(source_dir, self), path=source_dir, recursive=True)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{source_dir} 的目录监控服务启动")
                    except Exception as e:
                        err_msg = str(e)
                        if "inotify" in err_msg and "reached" in err_msg:
                            logger.warn(
                                f"目录监控服务启动出现异常：{err_msg}，请在宿主机上（不是docker容器内）执行以下命令并重启："
                                + """
                                     echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
                                     echo fs.inotify.max_user_instances=524288 | sudo tee -a /etc/sysctl.conf
                                     sudo sysctl -p
                                     """)
                        else:
                            logger.error(f"{source_dir} 启动目录监控失败：{err_msg}")
                        self.systemmessage.put(f"{source_dir} 启动目录监控失败：{err_msg}")

            # 运行一次定时服务
            if self._onlyonce:
                logger.info("短剧监控服务启动，立即运行一次")
                self._scheduler.add_job(func=self.sync_all, trigger='date',
                                        run_date=datetime.datetime.now(
                                            tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3),
                                        name="短剧监控全量执行")
                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __is_check_pass(self, path_str: str) -> bool:
        """
        检查路径是否需要跳过处理
        :param path_str: 路径字符串
        :return: 是否pass
        """

        if self._link_pass:
            # 检查是否是文件而且是硬链接
            if os.path.isfile(path_str):
                # 检查是否是硬链接
                if os.stat(path_str).st_nlink > 1:
                    #logger.debug(f"{path_str} 硬链接已整理，跳过处理")
                    return True

        # 过滤关键字
        if self._polish_keywords:
            for keyword in self._polish_keywords.split("\n"):
                if keyword and re.findall(keyword, path_str):
                    return False
            #logger.debug(f"{path_str} 未命中整理关键字，跳过处理")
            return True

        if self._exclude_keywords:
            for keyword in self._exclude_keywords.split("\n"):
                if keyword and re.findall(keyword, path_str):
                    #logger.debug(f"{path_str} 命中过滤关键字 {keyword}，跳过处理")
                    return True

        return False

    def sync_all(self):
        """
        立即运行一次，全量同步目录中所有文件
        """
        logger.info("开始全量同步短剧监控目录 ...")
        # 遍历所有监控目录
        for mon_path in self._dirconf.keys():
            # 遍历目录下所有文件
            for file_path in SystemUtils.list_files(Path(mon_path), settings.RMT_MEDIAEXT):
                if self.__is_check_pass(str(file_path)):
                    continue
                self.__handle_file(is_directory=Path(file_path).is_dir(),
                                   event_path=str(file_path),
                                   source_dir=mon_path)
        logger.info("全量同步短剧监控目录完成！")

    def event_handler(self, event, source_dir: str, event_path: str):
        """
        处理文件变化
        :param event: 事件
        :param source_dir: 监控目录
        :param event_path: 事件文件路径
        """
        # 回收站及隐藏的文件不处理
        if (event_path.find("/@Recycle") != -1
                or event_path.find("/#recycle") != -1
                or event_path.find("/@eaDir") != -1):
            logger.info(f"{event_path} 是回收站的文件，跳过处理")
            return

        if self.__is_check_pass(event_path):
            return

        # 不是媒体文件不处理
        if Path(event_path).suffix not in settings.RMT_MEDIAEXT:
            logger.debug(f"{event_path} 不是媒体文件")
            return

        # 文件发生变化
        logger.debug(f"变动类型 {event.event_type} 变动路径 {event_path}")
        self.__handle_file(is_directory=event.is_directory,
                           event_path=event_path,
                           source_dir=source_dir)

    def __to_pinyin_with_title(self, s):
        if not s:
            return ""

        p = Pinyin()
        pinyin_list = []
        for z in s:
            pinyin_list.append(p.get_pinyin(z, '').title())

        return ' '.join(pinyin_list).replace('，', ',')

    def __meta_search_tv_name(self, file_meta: MetaInfoPath, tv_name: str, is_compilations: bool = False):
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

        if is_compilations:
            file_meta.org_string = title
            file_meta.subtitle = subtitle
            file_meta.cn_name = title
            file_meta.en_name = self.__to_pinyin_with_title(title)
            file_meta.begin_season = 0
            file_meta.total_season = 1
            file_meta.total_episode = 1
            file_meta.begin_episode = 1
        else:
            file_meta.org_string = title
            file_meta.subtitle = subtitle
            file_meta.cn_name = title
            file_meta.en_name = self.__to_pinyin_with_title(title)
            file_meta.begin_season = 1
            file_meta.total_season = 1
            file_meta.total_episode = episodes
        return file_meta

    def __meta_complement(self, is_directory: bool, media_path: str):
        _path = Path(media_path)
        file_meta = MetaInfoPath(_path)
        if _path.parent.name in ['合集', '长篇', '长篇合集', '合集长篇']:
            logger.info(f"合集目录，查看父目录是否是电视剧目录：{media_path}")
            parent_dir = _path.parent
            is_tv = False
            for file in parent_dir.iterdir():
                if file.suffix.lower() in settings.RMT_MEDIAEXT:
                    is_tv = True
                    break
            if not is_tv:
                logger.info(f"父目录不是电视剧目录，不处理：{media_path}")
                return None
            tv_name = parent_dir.parent.name
            file_meta = self.__meta_search_tv_name(file_meta, tv_name, True)
        elif re.search(r'^\d+(-\d+)?([集话]|本季完|完结|最终集|大结局)?$', file_meta.org_string):
            if is_directory:
                logger.warn(f"单独的数字目录，不处理：{media_path}")
                return None
            tv_name = Path(media_path).parent.name
            if tv_name == "分集":
                tv_name = Path(media_path).parent.parent.name
            file_meta = self.__meta_search_tv_name(file_meta, tv_name)

        if self._rename_title:
            rename_titles = self._rename_title.split("\n")
            if not rename_titles:
                return file_meta
            for rename_title in rename_titles:
                if not '=>' in rename_title:
                    continue
                old_title = rename_title.split('=>')[0].strip()
                new_title = rename_title.split('=>')[1].strip()
                if StringUtils.is_chinese(new_title):
                    if old_title in file_meta.cn_name:
                        logger.info(f"替换标题：{file_meta.cn_name} => {new_title}")

                        file_meta.cn_name = new_title
                        file_meta.en_name = self.__to_pinyin_with_title(new_title)
                    break
                else:
                    if old_title in file_meta.en_name:
                        logger.info(f"替换标题：{file_meta.en_name} => {new_title}")
                        file_meta.cn_name = None
                        file_meta.en_name = old_title
                    break
        return file_meta

    def __handle_file(self, is_directory: bool, event_path: str, source_dir: str):
        """
        同步一个文件
        :event.is_directory
        :param event_path: 事件文件路径
        :param source_dir: 监控目录
        """
        # 转移路径
        dest_dir = self._dirconf.get(source_dir)
        # 元数据
        file_meta = self.__meta_complement(is_directory, event_path)
        if not file_meta:
            return
        metadict = file_meta.to_dict()
        logger.debug(f"元数据：{metadict}")
        if not file_meta.name:
            logger.error(f"{Path(event_path).name} 无法识别有效信息")
            return

        try:
            # 从选择的站点识别媒体信息
            mediainfo = self._site_cache.get(file_meta.cn_name)
            if not mediainfo:
                logger.info(f"从选择的站点 {self._searchsites} 识别媒体信息")
                mediainfo = self._sites_recognize_media(file_meta)
                if mediainfo:
                    self._site_cache[file_meta.cn_name] = mediainfo

            if not mediainfo:
                logger.error(f"选择的站点未查找到媒体信息")
                return

            # 短剧合集放在特殊季
            if file_meta.begin_season == 0:
                mediainfo.season = 0
            logger.debug(f"媒体信息：{mediainfo}")

            if file_meta.cn_name in self._name_error_cache.keys():
                if self._name_error_cache.get(file_meta.cn_name) > self._error_count:
                    logger.info(f"剧名（{file_meta.cn_name}）搜索错误次数超过{self._error_count}次")
                    return
            else:
                self._name_error_cache[file_meta.cn_name] = 0
            try:
                # 查询转移目的目录
                target_dir = DirectoryHelper().get_dir(mediainfo, src_path=Path(source_dir))
                if not target_dir or not target_dir.library_path:
                    target_dir = TransferDirectoryConf()
                    target_dir.library_path = dest_dir
                    target_dir.transfer_type = self._transfer_type
                    target_dir.renaming = True
                    target_dir.notify = False
                    target_dir.overwrite_mode = 'never'
                    target_dir.library_storage = self._storage_type
                else:
                    target_dir.transfer_type = self._transfer_type

                if not target_dir.library_path:
                    logger.error(f"未配置监控目录 {source_dir} 的目的目录")
                    return

                # 整理
                fileitem = StorageChain().get_file_item(self._storage_type, Path(event_path))
                logger.debug(f"fileitem：{fileitem}")
                if '&' in event_path:
                    fileitem.path = fileitem.path.replace('/&', '\&')
                    logger.info(f"路径：{fileitem.path}")
                _end_episode = file_meta.end_episode # 传输会将end_episode清空，先保存
                transferinfo: TransferInfo = self.chain.transfer(mediainfo=mediainfo,
                                                                fileitem=fileitem,
                                                                target_directory=target_dir,
                                                                meta=file_meta)
                file_meta.end_episode = _end_episode
                if not transferinfo:
                    logger.error("文件转移模块运行错误")
                    return
                if not transferinfo.success:
                    logger.warn(transferinfo.message)
                    return

            except Exception as e:
                print(str(e))
                logger.error(f"{event_path} 刮削失败, 请重新配置运行插件")
                self.stop_service()
                return

            self._site_scrape_metadata(file_meta, transferinfo, mediainfo=mediainfo)

            self._name_error_cache[file_meta.cn_name] = 0

            # 广播事件
            # self.eventmanager.send_event(EventType.TransferComplete, {
            #     'meta': file_meta,
            #     'mediainfo': mediainfo,
            #     'transferinfo': transferinfo
            # })
            if self._notify:
                # 发送消息汇总
                media_list = self._medias.get(mediainfo.title_year) or {}
                if media_list:
                    media_files = media_list.get("files") or []
                    if media_files:
                        if str(event_path) not in media_files:
                            media_files.append(str(event_path))
                    else:
                        media_files = [str(event_path)]
                    media_list = {
                        "files": media_files,
                        "time": datetime.datetime.now()
                    }
                else:
                    media_list = {
                        "files": [str(event_path)],
                        "time": datetime.datetime.now()
                    }
                self._medias[mediainfo.title_year] = media_list
        except Exception as e:
            self._name_error_cache[file_meta.cn_name] = self._site_cache.get(file_meta.cn_name) + 1
            logger.error(f"event_handler_created error: {e}")
            print(str(e))

    def _site_comparison_meta(self, name, tv_name):
        tv_name = re.sub(r'（', '(', re.sub(r'）', ')', tv_name))
        tv_name = re.sub(r'＆', '&', tv_name)
        match = re.match(r'^(.*?)\(([全共]?\d+)[集话話期幕]\)(?:&?([^&]+))?', tv_name)
        if match:
            title = match.group(1).strip().split('(')[0]
            if name and name == title:
                return True
            elif len(name) > 6 and name in title:
                    return True
        return False

    def _site_meta_update(self, meta, info, cover: bool = False):
        if not info.get('org_string') or cover:
            info['org_string'] = meta.org_string
        if not info.get('cn_name') or cover:
            info['cn_name'] = meta.cn_name
        if not info.get('en_name') or cover:
            info['en_name'] = meta.en_name
        if not info.get('subtitle') or cover:
            info['subtitle'] = meta.subtitle
        if not info.get('begin_season') or cover:
            info['begin_season'] = meta.begin_season
        if not info.get('total_season') or cover:
            info['total_season'] = meta.total_season
        if not info.get('total_episode') or cover:
            info['total_episode'] = meta.total_episode
        if not info.get('season_episode') or cover:
            info['season_episode'] = "S01"
        return info

    def _site_get_context(self, meta):
        site_contexts = []

        torrents = SearchChain().last_search_results()
        if torrents:
            for torrent in torrents:
                _context = torrent.to_dict()
                logger.debug(f"context: {_context}")
                if (meta.en_name and meta.en_name == _context.get('meta_info').get('en_name')) or (meta.cn_name and meta.cn_name == _context.get('meta_info').get('cn_name')):
                    _context['meta_info'] = self._site_meta_update(meta, _context.get('meta_info'))
                    site_contexts.append(_context)
                else:
                    if self._site_comparison_meta(meta.cn_name, _context.get('meta_info').get('org_string')):
                        _context['meta_info'] = self._site_meta_update(meta, _context.get('meta_info'), True)
                        site_contexts.append(_context)
        if len(site_contexts) == 0:
            if meta.cn_name in self._search_error_cache.keys():
                if self._search_error_cache.get(meta.cn_name) > self._error_count:
                    logger.warn(f"种子访问失败次数超过{self._error_count}次：{meta.cn_name}")
                    return {}
            else:
                self._search_error_cache[meta.cn_name] = 0

            torrents = SearchChain().search_by_title(title=meta.cn_name, sites=self._searchsites, cache_local=True)
            if torrents:
                for torrent in torrents:
                    _context = torrent.to_dict()
                    logger.debug(f"context: {_context}")
                    if meta.en_name == _context.get('meta_info').get('en_name') or meta.cn_name == _context.get('meta_info').get('cn_name'):
                        _context['meta_info'] = self._site_meta_update(meta, _context.get('meta_info'))
                        site_contexts.append(_context)
                    else:
                        if self._site_comparison_meta(meta.cn_name, _context.get('meta_info').get('org_string')):
                            _context['meta_info'] = self._site_meta_update(meta, _context.get('meta_info'), True)
                            site_contexts.append(_context)
            else:
                self._search_error_cache[meta.cn_name] = self._search_error_cache.get(meta.cn_name) + 1
                return {}

        site_contexts_year = []
        if len(site_contexts) == 0:
            self._search_error_cache[meta.cn_name] = self._search_error_cache.get(meta.cn_name) + 1
            return {}

        self._search_error_cache[meta.cn_name] = 0
        if len(site_contexts) == 1:
            return site_contexts[0]
        else:
            _year_priority = []
            _year_null = []
            # 有year的放前面
            for _m in site_contexts:
                if _m.get('meta_info').get('year'):
                    _year_priority.append(_m)
                else:
                    _year_null.append(_m)
            if _year_priority:
                _year_priority.extend(_year_null)
            else:
                _year_priority = _year_null
            site_contexts = _year_priority
            if meta.year:
                for _m in site_contexts:
                    if meta.year == _m.get('meta_info').get('year'):
                        site_contexts_year.append(_m)

        site_contexts_edition = []
        if len(site_contexts_year) == 0:
            return site_contexts[0]

        if len(site_contexts_year) == 1:
            return site_contexts_year[0]
        else:
            _edition_priority = []
            _edition_null = []
            # 有edition的放前面
            for _m in site_contexts_year:
                if _m.get('meta_info').get('edition'):
                    _edition_priority.append(_m)
                else:
                    _edition_null.append(_m)
            if _edition_priority:
                _edition_priority.extend(_edition_null)
            else:
                _edition_priority = _edition_null
            site_contexts_year = _edition_priority
            if meta.edition:
                for _m in site_contexts_year:
                    if not meta.edition and meta.edition == _m.get('meta_info').get('edition'):
                        site_contexts_edition.append(_m)

        site_contexts_season = []
        if len(site_contexts_edition) == 0:
            return site_contexts_year[0]

        if len(site_contexts_edition) == 1:
            return site_contexts_edition[0]
        else:
            for _m in site_contexts_edition:
                if meta.is_in_season(_m.get('meta_info').get('season_episode')):
                    site_contexts_season.append(_m)

        if len(site_contexts_edition) == 0:
            return site_contexts_edition[0]
        else:
            return site_contexts_season[0]

    def _site_brief_text(self, torrent):
        site = SiteOper().get(torrent.get('site'))
        url = torrent.get("page_url")
        # 获取种子详情页
        torrent_detail_source = self.__get_page_source(url=url, site=site)
        if not torrent_detail_source:
            logger.error(f"请求种子详情页失败 {url}")
            return None

        html = etree.HTML(torrent_detail_source)
        if not html:
            logger.error(f"详情页无数据 {url}")
            return None

        return html

    def _sites_recognize_media(self, meta):
        context = self._site_get_context(meta)
        if not context:
            return None

        html = self._site_brief_text(context.get('torrent_info'))
        brief_texts = html.xpath("//td[contains(@class, 'rowfollow')]/div[@id='kdescr']")
        if not brief_texts:
            return None

        img_url = None
        images = brief_texts[0].xpath(".//img[1]/@src")
        if not images:
            logger.error(f"未获取到种子封面图 {context.get('torrent_info').get("page_url")}")
        else:
            img_url = str(images[0])
        logger.info(f"获取到种子封面图 {img_url}")

        brief_text = brief_texts[0].xpath("string()").strip()
        brief = None
        if brief_text and len(brief_text) > 5:
            brief_match = re.search(r'◎简\s*介\s*([^◎]*)', brief_text)
            if brief_match:
                brief = brief_match.group(1).strip()
            else:
                brief = brief_text.strip()

        subtitle = context.get('meta_info').get('subtitle')
        tags = []
        if subtitle and '类型' in subtitle:
            subtitle_strs = subtitle.split('|')
            for s in subtitle_strs:
                if '类型' in s:
                    tags = s.replace('类型', '').replace(':', '').replace('：', '').split()
        if 'labels' in context.get('torrent_info').keys():
            for _l in context.get('torrent_info').get('labels'):
                if '禁转' in _l or '官方' in _l or '短剧' in _l or re.search(r'\d+[月天时分]', _l):
                    pass
                else:
                    tags.append(_l)

        actors = []
        if subtitle and '演员' in subtitle:
            subtitle_strs = subtitle.split('|')
            for s in subtitle_strs:
                if '演员' in s:
                    actors = [{ 'name': elem, 'type': 'Actor' } for elem in s.replace('演员', '').replace(':', '').replace('：', '').split()]

        logger.info(f"获取tag: {tags}")
        logger.debug(f"简介: {brief}")

        mediainfo = MediaInfo()
        mediainfo.source = 'site'
        mediainfo.type = MediaType.TV
        mediainfo.title = context.get('meta_info').get('cn_name')
        mediainfo.en_title = context.get('meta_info').get('en_name')
        mediainfo.year = context.get('meta_info').get('year')
        if not mediainfo.year:
            datetime_object = datetime.datetime.strptime(context.get('torrent_info').get('pubdate'), '%Y-%m-%d %H:%M:%S')
            mediainfo.year = datetime_object.year

        mediainfo.season = int(context.get('meta_info').get('season_episode').replace('S', ''))
        mediainfo.original_title = context.get('meta_info').get('cn_name')
        mediainfo.poster_path = img_url
        mediainfo.category = "短剧"
        mediainfo.number_of_episodes = context.get('meta_info').get('total_episode')
        mediainfo.number_of_seasons = context.get('meta_info').get('total_season')
        mediainfo.overview = brief
        mediainfo.mediaid_prefix = context.get('torrent_info').get('site_name')
        mediainfo.media_id = context.get('torrent_info').get('site')
        mediainfo.release_date = context.get('torrent_info').get('pubdate')
        mediainfo.tagline = tags
        mediainfo.actors = actors

        return mediainfo

    def _site_save_all_img(self, thumb_path, transferinfo, season: int):
        backdrop_path = f"{transferinfo.target_diritem.path}poster.jpg"
        if not os.path.exists(backdrop_path):
            logger.debug(f"保存背景图片：{backdrop_path}")
            self.__save_poster(input_path=thumb_path, poster_path=backdrop_path, cover_conf="16:9")
        folder_path = f"{transferinfo.target_diritem.path}folder.jpg"
        if not os.path.exists(folder_path):
            logger.debug(f"保存文件夹图片：{folder_path}")
            self.__save_poster(input_path=thumb_path, poster_path=folder_path, cover_conf="2:3")
        landscape_path = f"{transferinfo.target_diritem.path}landscape.jpg"
        if not os.path.exists(landscape_path):
            logger.debug(f"保存风景图片：{landscape_path}")
            self.__save_poster(input_path=thumb_path, poster_path=landscape_path, cover_conf="16:9")
        poster_path = f"{transferinfo.target_diritem.path}season{season:02d}-poster.jpg"
        if not os.path.exists(poster_path):
            logger.debug(f"保存海报图片：{poster_path}")
            self.__save_poster(input_path=thumb_path, poster_path=poster_path, cover_conf="2:3")
        episode_video_path = transferinfo.target_item.path
        _episode_video_path = Path(episode_video_path)
        episode_thumb_path = _episode_video_path.with_name(_episode_video_path.stem + "-thumb.jpg")
        if not os.path.exists(episode_thumb_path):
            logger.debug(f"保存每集图片：{episode_thumb_path}")
            self.get_thumb(episode_video_path, episode_thumb_path)

    def _site_scrape_metadata(self, file_meta, transferinfo, mediainfo):
        tv_path = transferinfo.target_diritem.path
        ep_path = transferinfo.target_item.path
        se_path = os.path.dirname(ep_path)
        name = transferinfo.target_item.basename
        match = re.search(r'S(\d{2})E(\d{2,})', name)
        if match:
            episode = int(match.group(2))
        else:
            episode = -1

        if not os.path.exists(f"{tv_path}/tvshow.nfo"):
            self.__gen_tv_nfo_file(Path(tv_path), mediainfo.title, mediainfo.year, mediainfo.overview, mediainfo.release_date, mediainfo.tagline, mediainfo.actors)
        if not os.path.exists(f"{se_path}/season.nfo"):
            self.__gen_se_nfo_file(Path(se_path), mediainfo.season, mediainfo.year, mediainfo.overview, mediainfo.release_date, mediainfo.actors)
        if not os.path.exists(f"{se_path}/{name}.nfo"):
            self.__gen_ep_nfo_file(Path(se_path), name, mediainfo.season, episode, mediainfo.year, date=mediainfo.release_date, end_episode=file_meta.end_episode)

        download_path = f"{tv_path}/download.jpg"
        file_path = Path(download_path)
        thumb_path = file_path.with_name(file_path.stem + "-site.jpg")
        if thumb_path.exists():
            logger.info(f"图片已下载：{thumb_path}")
            self._site_save_all_img(thumb_path, transferinfo, mediainfo.season)
        else:
            if self.__save_image(url=mediainfo.poster_path, file_path=thumb_path):
                self._site_save_all_img(thumb_path, transferinfo, mediainfo.season)

        #thumb_path.unlink()
        logger.info("图片搜刮完成")

    def send_msg(self):
        """
        定时检查是否有媒体处理完，发送统一消息
        """
        if self._notify:
            if not self._medias or not self._medias.keys():
                return

            # 遍历检查是否已刮削完，发送消息
            for medis_title_year in list(self._medias.keys()):
                media_list = self._medias.get(medis_title_year)
                logger.info(f"开始处理媒体 {medis_title_year} 消息")

                if not media_list:
                    continue

                # 获取最后更新时间
                last_update_time = media_list.get("time")
                media_files = media_list.get("files")
                if not last_update_time or not media_files:
                    continue

                # 判断剧集最后更新时间距现在是已超过10秒或者电影，发送消息
                if (datetime.datetime.now() - last_update_time).total_seconds() > int(self._interval):
                    # 发送消息
                    self.post_message(mtype=NotificationType.Organize,
                                      title=f"{medis_title_year} 共{len(media_files)}集已入库",
                                      text="类别：短剧")
                    # 发送完消息，移出key
                    del self._medias[medis_title_year]
                    continue

    def __save_poster(self, input_path, poster_path, cover_conf):
        """
        截取图片做封面
        """
        try:
            image = Image.open(input_path)

            # 需要截取的长宽比（比如 16:9）
            if not cover_conf:
                target_ratio = 2 / 3
            else:
                covers = cover_conf.split(":")
                target_ratio = int(covers[0]) / int(covers[1])

            # 获取原始图片的长宽比
            original_ratio = image.width / image.height

            # 计算截取后的大小
            if original_ratio > target_ratio:
                new_height = image.height
                new_width = int(new_height * target_ratio)
            else:
                new_width = image.width
                new_height = int(new_width / target_ratio)

            # 计算截取的位置
            left = (image.width - new_width) // 2
            top = (image.height - new_height) // 2
            right = left + new_width
            bottom = top + new_height

            # 截取图片
            cropped_image = image.crop((left, top, right, bottom))

            # 保存截取后的图片
            cropped_image.save(poster_path)
        except Exception as e:
            print(str(e))

    def __gen_tv_nfo_file(self, dir_path: Path, title: str, year: str, plot: str = None, date: str = None, tags: list = [], actors: dict = None):
        """
        生成电视剧总季的NFO描述文件
        :param dir_path: 电视剧根目录
        """
        # 开始生成XML
        logger.info(f"正在生成电视剧总季NFO文件：{dir_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "tvshow")

        current_time = datetime.datetime.now()
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")

        #介绍
        if plot:
            DomUtils.add_node(doc, root, "plot", plot)
            DomUtils.add_node(doc, root, "outline", plot)
        #日期
        if date:
            DomUtils.add_node(doc, root, "premiered", date)
            DomUtils.add_node(doc, root, "releasedate", date)
        # 标题
        DomUtils.add_node(doc, root, "dateadded", formatted_time)
        DomUtils.add_node(doc, root, "title", title)
        DomUtils.add_node(doc, root, "originaltitle", title)
        DomUtils.add_node(doc, root, "year", year)
        if tags:
            for _t in tags:
                DomUtils.add_node(doc, root, "tag", _t)
        if actors:
            actor = DomUtils.add_node(doc, root, "actor")
            for _a in actors:
                    DomUtils.add_node(doc, actor, "name", _a.get('name'))
                    DomUtils.add_node(doc, actor, "type", _a.get('type'))

        DomUtils.add_node(doc, root, "season", "-1")
        DomUtils.add_node(doc, root, "episode", "-1")
        # 保存
        self.__save_nfo(doc, dir_path.joinpath("tvshow.nfo"))

    def __gen_se_nfo_file(self, dir_path: Path, season: int, year: str, plot: str = None, date: str = None, actors: dict = None):
        """
        生成电视剧季的NFO描述文件
        :param dir_path: 电视剧季目录
        """
        # 开始生成XML
        logger.info(f"正在生成电视剧季NFO文件：{dir_path.name}")
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "season")

        current_time = datetime.datetime.now()
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")

        #介绍
        if plot:
            DomUtils.add_node(doc, root, "plot", plot)
            DomUtils.add_node(doc, root, "outline", plot)
        #日期
        if date:
            DomUtils.add_node(doc, root, "premiered", date)
            DomUtils.add_node(doc, root, "releasedate", date)
        # 标题
        DomUtils.add_node(doc, root, "dateadded", formatted_time)
        DomUtils.add_node(doc, root, "title", f"第 {season} 季")
        DomUtils.add_node(doc, root, "year", year)
        DomUtils.add_node(doc, root, "seasonnumber", season)
        if actors:
            actor = DomUtils.add_node(doc, root, "actor")
            for _a in actors:
                    DomUtils.add_node(doc, actor, "name", _a.get('name'))
                    DomUtils.add_node(doc, actor, "type", _a.get('type'))
        # 保存
        self.__save_nfo(doc, dir_path.joinpath("season.nfo"))

    def __gen_ep_nfo_file(self, dir_path: Path, name: str, season: int, episode: int, year: str, plot: str = None, date: str = None, end_episode: int = None):
        """
        生成电视剧集的NFO描述文件
        :param dir_path: 电视剧集目录
        """
        # 开始生成XML
        doc = minidom.Document()
        root = DomUtils.add_node(doc, doc, "episodedetails")

        current_time = datetime.datetime.now()
        formatted_time = current_time.strftime("%Y-%m-%d %H:%M:%S")

        #介绍
        if plot:
            DomUtils.add_node(doc, root, "plot", plot)
        #日期
        if date:
            DomUtils.add_node(doc, root, "aired", date)
        DomUtils.add_node(doc, root, "dateadded", formatted_time)
        # 标题

        if end_episode:
            DomUtils.add_node(doc, root, "title", f"第 {episode}-{end_episode} 集")
        else:
            DomUtils.add_node(doc, root, "title", f"第 {episode} 集")
        DomUtils.add_node(doc, root, "year", year)
        DomUtils.add_node(doc, root, "season", season)
        DomUtils.add_node(doc, root, "episode", episode)
        # 保存
        self.__save_nfo(doc, dir_path.joinpath(f"{name}.nfo"))

    def __save_nfo(self, doc, file_path: Path):
        """
        保存NFO
        """
        xml_str = doc.toprettyxml(indent="  ", encoding="utf-8")
        file_path.write_bytes(xml_str)
        logger.info(f"NFO文件已保存：{file_path}")

    @retry(RequestException, logger=logger)
    def __save_image(self, url: str, file_path: Path):
        """
        下载图片并保存
        """
        if url in self._img_error_cache.keys():
            if self._img_error_cache.get(url) > self._error_count:
                logger.info(f"图片下载失败次数超过{self._error_count}次：{url}")
                return False
        else:
            self._img_error_cache[url] = 0

        try:
            logger.info(f"正在下载{file_path.stem}图片：{url} ...")
            r = RequestUtils().get_res(url=url, raise_exception=True)
            if r:
                file_path.write_bytes(r.content)
                logger.debug(f"图片已保存：{file_path}")
                self._img_error_cache[url] = 0
                return True
            else:
                logger.warn(f"{file_path.stem}图片下载失败，请检查网络连通性")
                self._img_error_cache[url] = self._img_error_cache.get(url) + 1
                return False
        except RequestException as err:
            raise err
        except Exception as err:
            logger.error(f"{file_path.stem}图片下载失败：{str(err)}")
            self._img_error_cache[url] = self._img_error_cache.get(url) + 1
            return False

    def __get_page_source(self, url: str, site):
        """
        获取页面资源
        """
        ret = RequestUtils(
            cookies=site.cookie,
            timeout=30,
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

        return page_source

    @staticmethod
    def get_thumb(video_path: str, image_path: str, frames: str = None):
        """
        使用ffmpeg从视频文件中截取缩略图
        """
        if not frames:
            frames = "00:00:10"
        if not video_path or not image_path:
            return False
        cmd = 'ffmpeg -y -i "{video_path}" -ss {frames} -frames 1 "{image_path}"'.format(
            video_path=video_path,
            frames=frames,
            image_path=image_path)
        result = SystemUtils.execute(cmd)
        if result:
            return True
        return False

    def __update_config(self):
        """
        更新配置
        """
        self.update_config({
            "enabled": self._enabled,
            "polish_keywords": self._polish_keywords,
            "exclude_keywords": self._exclude_keywords,
            "transfer_type": self._transfer_type,
            "storage_type": self._storage_type,
            "searchsites": self._searchsites,
            "onlyonce": self._onlyonce,
            "link_pass": self._link_pass,
            "interval": self._interval,
            "notify": self._notify,
            "monitor_confs": self._monitor_confs,
            "rename_title": self._rename_title,
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
        # 站点选项
        site_options = [{"title": site.get("name"), "value": site.get("id")}
                        for site in SitesHelper().get_indexers()]
        storage_list = [{'title': storage.name, 'value': storage.value} for storage in StorageSchema]
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
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                            'model': 'link_pass',
                                            'label': '排除硬链接',
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
                            },
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
                                    'md': 5
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'transfer_type',
                                            'label': '转移方式',
                                            'items': [
                                                {'title': '移动', 'value': 'move'},
                                                {'title': '复制', 'value': 'copy'},
                                                {'title': '硬链接', 'value': 'link'},
                                                {'title': '软链接', 'value': 'softlink'},
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 5
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'storage_type',
                                            'label': '存储类型',
                                            'items': storage_list
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 2
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval',
                                            'label': '入库消息延迟',
                                            'placeholder': '10'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'monitor_confs',
                                            'label': '监控目录',
                                            'rows': 3,
                                            'placeholder': '监控方式#监控目录#目的目录'
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'rename_title',
                                            'label': '标题重命名',
                                            'rows': 2,
                                            'placeholder': '原标题=>新标题'
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
                                            'model': 'polish_keywords',
                                            'label': '整理关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
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
                                            'model': 'exclude_keywords',
                                            'label': '排除关键词',
                                            'rows': 2,
                                            'placeholder': '每一行一个关键词'
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
                                            'text': '配置说明：监控方式：1.fast:性能模式，内部处理系统操作类型选择最优解。2.compatibility:兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB （建议使用）'
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
                                            'text': '优先使用tmdb识别刮削，失败后从选择的站点识别刮削'
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
            "onlyonce": False,
            "link_pass": False,
            "notify": False,
            "interval": 10,
            "monitor_confs": "",
            "rename_title": "",
            "polish_keywords": "",
            "exclude_keywords": "",
            "transfer_type": "link",
            "storage_type": "local"
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
        self._observer = []
