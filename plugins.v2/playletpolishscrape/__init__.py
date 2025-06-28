import datetime
import os
import re
import filecmp
import concurrent.futures
from pathlib import Path
from typing import Any, List, Dict, Tuple, Optional
from xml.dom import minidom
from xpinyin import Pinyin

import pytz
from PIL import Image
from apscheduler.schedulers.background import BackgroundScheduler
from requests import RequestException
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver
from app.helper.sites import SitesHelper

from app.chain.storage import StorageChain
from app.chain.media import MediaChain
from app.core.config import settings
from app.core.metainfo import MetaInfoPath
from app.db.transferhistory_oper import TransferHistoryOper
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
    plugin_version = "2.2.1"
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
    _force = False
    _historysave = True
    _onlyscrape = False
    _update = False
    _fixlink = False
    _exclude_keywords = ""
    _polish_keywords = ""
    _transfer_type = "link"
    _storage_type = StorageSchema.Local.value
    _observer = []
    _timeline = "00:00:10"
    _transferhis = TransferHistoryOper()
    _dirconf = {}
    _coverconf = {}
    _interval = 10
    _collection_size = 200
    _notify = False
    _medias = {}
    _error_count = 5
    _img_error_cache = {}
    _search_error_cache = {}
    _name_error_cache = {}
    _is_stoped = False
    _thread_pool = None
    _storagechain = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 清空配置
        self._dirconf = {}
        self._coverconf = {}
        self._img_error_cache = {}
        self._search_error_cache = {}
        self._name_error_cache = {}
        self._storagechain = StorageChain()

        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._link_pass = config.get("link_pass")
            self._interval = int(config.get("interval"))
            self._collection_size = int(config.get("collection_size"))
            self._notify = config.get("notify")
            self._force = config.get("force")
            self._historysave = config.get("historysave")
            self._onlyscrape = config.get("onlyscrape")
            self._update = config.get('update')
            self._fixlink = config.get('fixlink')
            self._monitor_confs = config.get("monitor_confs")
            self._rename_title = config.get("rename_title")
            self._exclude_keywords = config.get("exclude_keywords") or ""
            self._polish_keywords = config.get("polish_keywords") or ""
            self._transfer_type = config.get("transfer_type") or "link"
            self._storage_type = config.get("storage_type") or StorageSchema.Local.value

        # 停止现有任务
        self.stop_service()

        if self._enabled or self._onlyonce:
            self._is_stoped = False
            # 线程池
            self._thread_pool = concurrent.futures.ThreadPoolExecutor(max_workers=6)
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
                # 格式 监控方式#监控目录#目的目录
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
                        logger.debug(f"无法监控 {e}")
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

        def _polish_check_pass(keywords, path_str):
            for keyword in keywords:
                if keyword and re.findall(keyword, path_str):
                    return False
            return True

        try:
            if self._link_pass:
                # 检查是否是文件而且是硬链接
                if os.path.isfile(path_str):
                    # 检查是否是硬链接
                    if os.stat(path_str).st_nlink > 1:
                        #logger.debug(f"{path_str} 硬链接已整理，跳过处理")
                        return True

            # 过滤关键字
            if self._polish_keywords:
                if _polish_check_pass(self._polish_keywords.split("\n"), path_str):
                    return True

            if self._exclude_keywords:
                for keyword in self._exclude_keywords.split("\n"):
                    if keyword and re.findall(keyword, path_str):
                        #logger.debug(f"{path_str} 命中过滤关键字 {keyword}，跳过处理")
                        return True
        except Exception as e:
            logger.error(f"跳过，因为检查路径是否需要跳过出错了：{e}")
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
                if self._is_stoped:
                    return

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
        if self._is_stoped:
            return

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

    def __meta_complement(self, is_directory: bool, media_path: str):

        def _meta_rename_title(file_meta):
            if self._rename_title:
                rename_titles = self._rename_title.split("\n")
                if not rename_titles:
                    return file_meta

                for rename_title in rename_titles:
                    if not '=>' in rename_title:
                        continue
                    old_title = rename_title.split('=>')[0].strip()
                    new_title = rename_title.split('=>')[1].strip().replace('$', ' ').replace('&', ' ').strip()
                    if StringUtils.is_chinese(new_title):
                        if file_meta.cn_name and re.search(rf"{old_title}", file_meta.cn_name):
                            logger.info(f"替换标题：{file_meta.cn_name} => {new_title}")

                            file_meta.cn_name = new_title
                            file_meta.en_name = to_pinyin_with_title(new_title)
                            break
                    else:
                        if file_meta.en_name and re.search(rf"{old_title}", file_meta.en_name):
                            logger.info(f"替换标题：{file_meta.en_name} => {new_title}")
                            file_meta.cn_name = None
                            file_meta.en_name = old_title
                            break

            return file_meta

        def _to_chinese_tv_name(title: str) -> str:
            tv_name = ""
            for word in re.split(r'[ .-]', title):
                if StringUtils.is_chinese(word):
                    tv_name += f"{word} "
            logger.info(f'chinese_tv_name={tv_name}')
            return tv_name.strip()

        _path = Path(media_path)
        tv_path = _path
        file_meta = MetaInfoPath(_path)
        org_string = file_meta.org_string

        # 判断是不是合集，使用父目录名和文件大小判断
        if _path.parent.name in ['合集', '合集版', '长篇', '长篇版', '长篇合集', '合集长篇'] \
                    or (_path.is_file() and _path.stat().st_size >= self._collection_size * 1024 * 1024):
            logger.info(f"合集目录，查看父目录是否是电视剧目录：{media_path}")
            parent_dir = _path.parent
            is_tv = False
            for file in parent_dir.iterdir():
                if file.suffix.lower() in settings.RMT_MEDIAEXT:
                    is_tv = True
                    break
            if not is_tv:
                logger.info(f"父目录不是电视剧目录，不处理：{media_path}")
                return None, tv_path
            tv_path = parent_dir.parent
            tv_name = tv_path.name
            file_meta = meta_search_tv_name(file_meta, _to_chinese_tv_name(tv_name), True)
        elif file_meta.cn_name and file_meta.year:
            pass
        elif re.search(r'^\d+([.-][0-9a-zA-Z]+)?([.-]\d+)?([集话]|本季完|完结|最终集|大结局)?.?$', org_string) \
                    or re.search(r'^\d+[.-]([0-9a-zA-Z]+)-.*', org_string) \
                    or re.search(r'^[0-9a-zA-Z]*$', org_string):
            logger.info(f"文件名符合剧集目录：{org_string}")
            if is_directory:
                logger.warn(f"单独的数字目录，不处理：{media_path}")
                return None, tv_path

            tv_path = Path(media_path).parent
            tv_name = tv_path.name
            if tv_name == "分集":
                tv_path = Path(media_path).parent.parent
                tv_name = tv_path.name
            file_meta = meta_search_tv_name(file_meta, _to_chinese_tv_name(tv_name))
        elif file_meta.cn_name:
            file_meta = meta_search_tv_name(file_meta, _to_chinese_tv_name(org_string))
        elif file_meta.name and StringUtils.is_chinese(tv_path.parent.name):
            file_meta = meta_search_tv_name(file_meta, _to_chinese_tv_name(tv_path.parent.name))

        file_meta.customization = '短剧'

        logger.info(f"begin_episode={file_meta.begin_episode}")
        if file_meta.begin_episode is None:
            ep_match = re.search(r'^(\d+)\b', org_string) # 集数可能在开头
            if not ep_match:
                ep_match = re.search(r'(\d+)$', org_string) # 集数可能在结尾
            if not ep_match:
                ep_match = re.search(r'\((\d+)\)', org_string) # 集数可能在括号
            logger.info(f"ep_match={ep_match}")
            if ep_match:
                try:
                    file_meta.begin_episode = int(ep_match.group(1))
                except:
                    logger.error(f"文件名获取的集数错误({ep_match.group(1)})")
                if file_meta.total_episode > 0 and file_meta.begin_episode and str(file_meta.begin_episode) != org_string:
                    if file_meta.begin_episode > file_meta.total_episode:
                        logger.error(f"文件名获取的集数错误: 集数({file_meta.begin_episode}) > 总集({file_meta.total_episode})")
                        file_meta.begin_episode = None

        return _meta_rename_title(file_meta), str(tv_path)

    def __handle_file(self, is_directory: bool, event_path: str, source_dir: str):
        """
        同步一个文件
        :event.is_directory
        :param event_path: 事件文件路径
        :param source_dir: 监控目录
        """
        logger.info(f"开始处理媒体文件：{event_path}")
        # 整理成功的不再处理
        if not self._force:
            transferd = self._transferhis.get_by_src(event_path, storage=self._storage_type)
            if transferd:
                if not transferd.status:
                    logger.info(f"{event_path} 已整理过，如需重新处理，请删除整理记录。")
                    return

        # 转移路径
        dest_dir = self._dirconf.get(source_dir)
        fileitem = self._storagechain.get_file_item(self._storage_type, Path(event_path))
        logger.debug(f"fileitem：{fileitem}")
        # 元数据
        try:
            file_meta, tv_path = self.__meta_complement(is_directory, event_path)
        except Exception as e:
            logger.error(f"识别元数据出错：{e}")
            return None
        if not file_meta:
            return
        metadict = file_meta.to_dict()
        logger.debug(f"元数据：{metadict}")
        if not file_meta.name:
            logger.error(f"{Path(event_path).name} 无法识别有效信息")
            return

        # 检查是否有集数
        if file_meta.begin_episode is None:
            # 如果有title和year，说明是连续剧，将集数设置为1
            if file_meta.name and file_meta.year:
                file_meta.begin_episode = 1
            else:
                # 没有集数且目录里只有一个媒体文件，将集数设置为1
                if is_directory:
                    _fileitem = fileitem
                else:
                    _fileitem = self._storagechain.get_file_item(self._storage_type, Path(event_path).parent)
                dir_fileitems = self._storagechain.list_files(_fileitem)
                _media_num = 0
                for fi in dir_fileitems:
                    if f'.{fi.extension}' in settings.RMT_MEDIAEXT:
                        _media_num += 1
                if _media_num == 1:
                    file_meta.begin_episode = 1

        try:
            # 从选择的站点识别媒体信息
            _begin_season = file_meta.begin_season # 识别会将begin_season修改，先保存
            if self._update:
                use_cache = False
                self._update = False
                self.__update_config()
            else:
                use_cache = True
            mediainfo = self.chain.recognize_media(meta=file_meta, mtype=MediaType.TV, cache=use_cache)
            if not mediainfo:
                logger.error(f"未查找到媒体信息")
                if self._historysave:
                    # 新增转移失败历史记录
                    self._transferhis.add_fail(
                        fileitem=fileitem,
                        mode=self._transfer_type,
                        meta=file_meta,
                        mediainfo=None,
                        transferinfo=None
                    )
                return
            file_meta.begin_season = _begin_season # 恢复

            if not mediainfo.season:
                mediainfo.season = 1
            # 短剧合集放在特殊季
            if file_meta.begin_season == 0:
                mediainfo.season = 0
            logger.debug(f"媒体信息：{mediainfo}")

            if not StringUtils.is_all_chinese(mediainfo.title):
                file_meta = meta_search_tv_name(file_meta, mediainfo.title)
                mediainfo.title = file_meta.cn_name
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

                if self._force:
                    target_dir.overwrite_mode = 'always'

                if not target_dir.library_path:
                    logger.error(f"未配置监控目录 {source_dir} 的目的目录")
                    return

                # 整理
                if '&' in event_path:
                    fileitem.path = fileitem.path.replace('/&', '\&')
                    logger.info(f"路径：{fileitem.path}")
                if self._onlyscrape:
                    # 只刮削，就认为改文件已整理过
                    transferinfo: TransferInfo = TransferInfo()
                    transferinfo.success = True
                    transferinfo.transfer_type = self._transfer_type
                    transferinfo.target_item = StorageChain().get_file_item(self._storage_type, Path(fileitem.path).parent)
                    transferinfo.target_diritem = StorageChain().get_file_item(self._storage_type, Path(transferinfo.target_item.path).parent)
                else:
                    _end_episode = file_meta.end_episode # 传输会将end_episode清空，先保存
                    transferinfo: TransferInfo = self.chain.transfer(mediainfo=mediainfo,
                                                                    fileitem=fileitem,
                                                                    target_directory=target_dir,
                                                                    meta=file_meta)
                    file_meta.end_episode = _end_episode
                if not transferinfo:
                    logger.error("文件转移模块运行错误")
                    return

                # 检查媒体库同名文件是不是一致，一致使用硬链接处理文件，不增加失败记录了
                _link_check_ok = False
                if self._fixlink and not transferinfo.success and transferinfo.message.startswith('媒体库存在同名文件'):
                    src_file = transferinfo.fileitem.path
                    dest_file = transferinfo.target_item.path
                    temp_file = f"{src_file}.back"
                    logger.info("媒体库同名文件，检查文件大小 ...")
                    try:
                        if filecmp.cmp(src_file, dest_file):
                            os.rename(src_file, temp_file)
                            os.link(dest_file, src_file)
                            os.remove(temp_file)
                            _link_check_ok = True
                            logger.info(f"硬链接成功：{src_file} <-- {dest_file}")
                    except Exception as e:
                        logger.error(f"修复硬链接错误：{e}")
                        if os.path.exists(temp_file):
                            os.rename(temp_file, src_file)

                if not _link_check_ok:
                    if not self._historysave and not transferinfo.success:
                        logger.warn(f"{fileitem.name} 入库失败：{transferinfo.message}")
                        return

                    if self._historysave:

                        if not transferinfo.success:
                            # 转移失败
                            logger.warn(f"{fileitem.name} 入库失败：{transferinfo.message}")
                            # 新增转移失败历史记录
                            self._transferhis.add_fail(
                                fileitem=fileitem,
                                mode=transferinfo.transfer_type,
                                meta=file_meta,
                                mediainfo=mediainfo,
                                transferinfo=transferinfo
                            )
                            return
                        else:
                            # 新增转移成功历史记录
                            self._transferhis.add_success(
                                fileitem=fileitem,
                                mode=transferinfo.transfer_type,
                                meta=file_meta,
                                mediainfo=mediainfo,
                                transferinfo=transferinfo
                            )

            except Exception as e:
                logger.error(f"{event_path} 刮削失败, 请重新配置运行插件: {e}")
                self._is_stoped = True
                return

            # 查看tv_path路径下是否有jpg文件
            self.__scrape_metadata(file_meta, transferinfo, mediainfo=mediainfo, img_path=self.__get_dir_image(tv_path))

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
            logger.error(f"event_handler_created error: {e}")
            self._is_stoped = True

    def __scrape_all_img(self, thumb_path, transferinfo, season: int = 1, scraping_switchs: dict = None):
        '''
        保存刮削图片
        :param thumb_path: 下载好的缩略图路径
        :param transferinfo: 媒体整理的转移信息
        :param season: 短剧季数
        :param scraping_switchs: mp的刮削配置开关
        '''
        if scraping_switchs.get('tv_backdrop'):
            backdrop_path = f"{transferinfo.target_diritem.path}poster.jpg"
            if not os.path.exists(backdrop_path):
                logger.debug(f"保存电视剧背景图：{backdrop_path}")
                self.__save_poster(input_path=thumb_path, poster_path=backdrop_path, cover_conf="2:3")
        if scraping_switchs.get('tv_thumb') or scraping_switchs.get('tv_poster'):
            folder_path = f"{transferinfo.target_diritem.path}folder.jpg"
            if not os.path.exists(folder_path):
                logger.debug(f"保存电视剧缩略图：{folder_path}")
                self.__save_poster(input_path=thumb_path, poster_path=folder_path, cover_conf="2:3")
        if scraping_switchs.get('tv_banner'):
            landscape_path = f"{transferinfo.target_diritem.path}landscape.jpg"
            if not os.path.exists(landscape_path):
                logger.debug(f"保存电视剧横幅图：{landscape_path}")
                self.__save_poster(input_path=thumb_path, poster_path=landscape_path, cover_conf="16:9")
        if scraping_switchs.get('season_poster'):
            poster_path = f"{transferinfo.target_diritem.path}season{season:02d}-poster.jpg"
            if not os.path.exists(poster_path):
                logger.debug(f"保存季海报：{poster_path}")
                self.__save_poster(input_path=thumb_path, poster_path=poster_path, cover_conf="2:3")
        if scraping_switchs.get('episode_thumb'):
            episode_video_path = transferinfo.target_item.path
            _episode_video_path = Path(episode_video_path)
            episode_thumb_path = _episode_video_path.with_name(_episode_video_path.stem + "-thumb.jpg")
            if not os.path.exists(episode_thumb_path):
                logger.debug(f"保存每集图片：{episode_thumb_path}")
                self.__get_thumb(episode_video_path, episode_thumb_path)

    def __scrape_metadata(self, file_meta, transferinfo, mediainfo, img_path: str = None):
        '''
        从站点刮削图片
        :param file_meta: 文件元数据
        :param transferinfo:媒体整理的转移信息
        :param mediainfo: 媒体元数据
        '''
        tv_path = transferinfo.target_diritem.path
        ep_path = transferinfo.target_item.path
        se_path = os.path.dirname(ep_path)
        name = transferinfo.target_item.basename
        match = re.search(r'S(\d{2})E(\d{2,})', name)
        if match:
            episode = int(match.group(2))
        else:
            episode = -1

        try:
            scraping_switchs = MediaChain._get_scraping_switchs()
            if scraping_switchs.get('tv_nfo'):
                if not os.path.exists(f"{tv_path}/tvshow.nfo"):
                    tags = mediainfo.tagline.split() if mediainfo.tagline else []
                    self.__gen_tv_nfo_file(Path(tv_path), mediainfo.title, mediainfo.year, mediainfo.overview, mediainfo.release_date, tags, mediainfo.actors)
            if scraping_switchs.get('season_nfo'):
                if not os.path.exists(f"{se_path}/season.nfo"):
                    self.__gen_se_nfo_file(Path(se_path), mediainfo.season, mediainfo.year, mediainfo.overview, mediainfo.release_date, mediainfo.actors)
            if scraping_switchs.get('episode_nfo') and os.path.isfile(ep_path):
                if not os.path.exists(f"{se_path}/{name}.nfo"):
                    self.__gen_ep_nfo_file(Path(se_path), name, mediainfo.season, episode, mediainfo.year, date=mediainfo.release_date, end_episode=file_meta.end_episode)
        except Exception as e:
            logger.error(f"刮削nfo文件失败：{e}")
            raise e

        download_path = f"{tv_path}/download.jpg"
        file_path = Path(download_path)

        if not mediainfo.poster_path:
            logger.warn(f"{mediainfo.title} 未获取到图片")
            return

        if img_path:
            thumb_path = Path(img_path)
        else:
            thumb_path = file_path.with_name(file_path.stem + "-site.jpg")
        if thumb_path.exists():
            logger.debug(f"图片已存在/下载：{thumb_path}")
            self.__scrape_all_img(thumb_path, transferinfo, mediainfo.season, scraping_switchs)
        else:
            try:
                if self.__save_image(url=mediainfo.poster_path, file_path=thumb_path):
                    self.__scrape_all_img(thumb_path, transferinfo, mediainfo.season, scraping_switchs)
            except RequestException as e:
                logger.error(f"下载图片失败：{e}")

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
            # 如果截取后的高度小于原图的一半，从图片的下半部分截取
            if new_height < image.height / 2:
                top = image.height // 2
            else:
                top = (image.height - new_height) // 2
            left = (image.width - new_width) // 2
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
        :param title: 电视剧标题
        :param year: 电视剧年份
        :param plot: 电视剧简介
        :param date: 电视剧发行日期
        :param tags: 电视剧根标签
        :param actors: 电视剧根演员
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
        :param season: 电视剧季数
        :param year: 电视剧年份
        :param plot: 电视剧简介
        :param date: 电视剧发行日期
        :param actors: 电视剧根演员
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
        :param name: 电视剧集名字
        :param season: 电视剧季数
        :param episode: 电视剧集数
        :param year: 电视剧年份
        :param plot: 电视剧简介
        :param date: 电视剧发行日期
        :param end_episode: 电视剧多集整合的最后集数
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
            self._img_error_cache[url] = self._img_error_cache.get(url) + 1
            raise err
        except Exception as err:
            logger.error(f"{file_path.stem}图片下载失败：{str(err)}")
            self._img_error_cache[url] = self._img_error_cache.get(url) + 1
            return False

    def __get_thumb(self, video_path: str, image_path: str, frames: str = None):
        """
        使用ffmpeg从视频文件中截取缩略图
        """
        def _get_thumb_task(video_path, image_path, frames):
            if not frames:
                frames = self._timeline
            if not video_path or not image_path:
                return False
            cmd = f'ffmpeg -y -i "{video_path}" -ss "{frames}" -frames 1 "{image_path}"'
            result = SystemUtils.execute(cmd)
            if result:
                logger.warn(f"截取视频缩略图：{video_path}，信息：{result}")

        # 提交任务到线程池
        future = self._thread_pool.submit(_get_thumb_task, video_path, image_path, frames)
        if future:
            return True
        return False

    @staticmethod
    def __get_dir_image(dir_path: str):
        max_image = None
        max_size = 0
        # 支持的图片文件格式
        image_extensions = (".jpg", ".jpeg", ".png")
        tag_img = ["folder.jpg", "poster.jpg"]

        if not os.path.isdir(dir_path):
            dir_path = Path(dir_path).parent
        # 遍历目录中的文件
        for filename in os.listdir(dir_path):
            file_path = os.path.join(dir_path, filename)
            if os.path.isfile(file_path) and filename.lower().endswith(image_extensions):
                if filename.lower() in tag_img:
                    return file_path
                file_size = os.path.getsize(file_path)
                if file_size > max_size:
                    max_size = file_size
                    max_image = file_path

        return max_image

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
            "onlyonce": self._onlyonce,
            "onlyscrape": self._onlyscrape,
            "update": self._update,
            "fixlink": self._fixlink,
            "historysave": self._historysave,
            "force": self._force,
            "link_pass": self._link_pass,
            "interval": self._interval,
            "collection_size": self._collection_size,
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
                                            'model': 'historysave',
                                            'label': '保存历史记录',
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
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
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
                                            'model': 'onlyscrape',
                                            'label': '仅刮削',
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
                                            'model': 'force',
                                            'label': '强制整理',
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'collection_size',
                                            'label': '认定为合集的文件大小(M)',
                                            'placeholder': '200'
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
                                    'md': 6
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
                                    'md': 6
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
                                    'md': 4
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
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'update',
                                            'label': '更新一次缓存',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'fixlink',
                                            'label': '修复硬链接',
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
                                            'text': '使用mp识别流程，搭配"短剧自动识别"插件，可以从网站和站点识别'
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
            "force": False,
            "historysave": True,
            "onlyscrape": False,
            "update": False,
            "fixlink": False,
            "interval": 10,
            "collection_size": 200,
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
            if self._thread_pool:
                self._thread_pool.shutdown(wait=True)
                self._thread_pool = None
        except Exception as e:
            self._thread_pool = None
            logger.error(f"线程池关闭失败：{e}")

        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            self._scheduler = None
            logger.error(f"退出插件失败：{e}")

        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
                    logger.error(f"停止监听失败：{e}")
        self._observer = []

def to_pinyin_with_title(s):
    '''
    中文标题转拼音英文标题
    '''
    if not s:
        return ""

    p = Pinyin()
    pinyin_list = []
    for z in s:
        pinyin_list.append(p.get_pinyin(z, '').title())

    title = ""
    for world in pinyin_list:
        if world.isdigit():
            title += world
        else:
            title += f" {world} "

    return title.replace('，', ',').replace('  ', ' ').strip()

def meta_search_tv_name(file_meta: MetaInfoPath, tv_name: str, is_compilations: bool = False):
    '''
    针对短剧识别标题
    :param file_meta: 文件元数据
    :param tv_name: 电视剧名
    :param is_compilations: 是否合集
    :return 一个新的文件元数据
    '''

    org_string = tv_name
    tv_name = tv_name.strip('.')
    tv_name = re.sub(r'^\d+[-.]*', '', tv_name)
    tv_name = tv_name.replace('（', '(').replace('）', ')').replace('＆', '&')
    bracket_match = re.match(r'^【(.*)】$', tv_name)
    if bracket_match:
        tv_name = bracket_match.group(1)
    else:
        tv_name = re.sub(r"【.*】", '', tv_name)
    tv_name = re.sub(r"\[.*\]", '', tv_name)
    logger.info(f"尝试识别媒体信息：{tv_name}")
    match = re.match(r'^(.*?)\(([全共]?\d+)[集话話期幕][全完]?\)(?:&?(.+))?', tv_name)
    if match:
        title = match.group(1).strip()
        if title[0] == '(':
            title = title.split(')')[1]
        else:
            title = title.split('(')[0]
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
        match = re.search(r'《.+》', tv_name)
        if match:
            tv_name = match.group(0).replace('《', '').replace('》', '').strip()

        title = tv_name.split('(')[0]
        episodes = 0
        actor_list = []

    if '-' in file_meta.org_string:
        ep_match = re.search(r'^(\d+)[.-](\d+)\b', file_meta.org_string)
        if ep_match:
            try:
                file_meta.begin_episode = int(ep_match.group(1))
                file_meta.end_episode = int(ep_match.group(2))
                if file_meta.begin_episode == file_meta.end_episode:
                    file_meta.end_episode = None
            except:
                logger.error(f"文件名获取的集数错误({ep_match.group(1)}-{ep_match.group(2)})")

        else:
            ep_match = re.search(r'^(\d+)[.-]([0-9a-zA-Z]+)\b', file_meta.org_string)
            if ep_match:
                try:
                    file_meta.begin_episode = int(ep_match.group(1))
                except:
                    logger.error(f"文件名获取的集数错误({ep_match.group(1)})")

    if actor_list:
        actors = ' '.join(actor_list)
        subtitle = f"{title} | 演员：{actors}"
    else:
        subtitle = tv_name

    title = title.replace('$', ' ').replace('&', ' ').strip()

    file_meta.org_string = org_string
    file_meta.subtitle = subtitle
    file_meta.cn_name = title
    file_meta.en_name = to_pinyin_with_title(title)

    if is_compilations:
        file_meta.begin_season = 0
        file_meta.total_season = 1
        if file_meta.begin_episode is None:
            file_meta.begin_episode = 1
    else:
        file_meta.begin_season = 1
        file_meta.total_season = 1
        file_meta.total_episode = episodes
    return file_meta
