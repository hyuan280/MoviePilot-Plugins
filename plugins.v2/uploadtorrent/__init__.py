from typing import Any, List, Dict, Tuple, Optional, Union
from pathlib import Path
import os
import threading
from datetime import datetime, timedelta
import pytz

from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.background import BackgroundScheduler
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.db.site_oper import SiteOper
from app.plugins import _PluginBase
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.schemas.types import EventType
from app.utils.system import SystemUtils
from app.schemas import ServiceInfo
from app.helper.downloader import DownloaderHelper


class FileMonitorHandler(FileSystemEventHandler):
    """
    目录监控响应类
    """

    def __init__(self, dir_pair: str, sync: Any, **kwargs):
        super(FileMonitorHandler, self).__init__(**kwargs)
        self._dir_conf = dir_pair
        self.sync = sync

    def on_created(self, event):
        self.sync.upload_torrent([self._dir_conf])

    def on_moved(self, event):
        self.sync.upload_torrent([self._dir_conf])


class UploadTorrent(_PluginBase):
    # 插件名称
    plugin_name = "上传种子文件"
    # 插件描述
    plugin_desc = "选择下载器，上传本地种子到下载器"
    # 插件图标
    plugin_icon = "upload.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "uploadtorrent_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _observer = []
    _is_paused = True
    _notify = False
    _onlyonce = False
    _enabled = False
    _downloader = None
    _run_mothod = None
    _monitor_type = None
    _cron = None
    _torrent_dirs = None
    site = None
    torrent_helper = None
    downloader_helper = None

    # 退出事件
    _event = threading.Event()

    def init_plugin(self, config: dict = None):
        self.downloader_helper = DownloaderHelper()
        self.site = SiteOper()

        if config:
            self._enabled = config.get("enabled")
            self._is_paused = config.get("is_paused")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._run_mothod = config.get("run_mothod")
            self._monitor_type = config.get("monitor_type")
            self._cron = config.get("cron")
            self._torrent_dirs = config.get("torrent_dirs")
            self._downloader = config.get("downloader")

        if self.get_state() or self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._run_mothod == "monitor":
                for torrent_dir in str(self._torrent_dirs).split("\n"):
                    monitor_dir = torrent_dir.split(":")[0]
                    try:
                        if self._monitor_type == "compatibility":
                            # 兼容模式，目录同步性能降低且NAS不能休眠，但可以兼容挂载的远程共享目录如SMB
                            observer = PollingObserver(timeout=10)
                        else:
                            # 内部处理系统操作类型选择最优解
                            observer = Observer(timeout=10)
                        self._observer.append(observer)
                        observer.schedule(FileMonitorHandler(torrent_dir, self), path=monitor_dir, recursive=False)
                        observer.daemon = True
                        observer.start()
                        logger.info(f"{monitor_dir} 的目录监控服务启动")
                    except Exception as e:
                        self._enabled = False
                        self._onlyonce = False
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
                            logger.error(f"{monitor_dir} 启动目录监控失败：{err_msg}")
                        self.systemmessage.put(f"{monitor_dir} 启动目录监控失败：{err_msg}", title=self.plugin_name)
            elif self._run_mothod == "cron":
                # get_service 使用注册插件公共服务接口
                logger.info("启动定时服务 ...")

            if self._onlyonce:
                self._onlyonce = False
                logger.info(f"上传种子，立即运行一次")
                self._scheduler.add_job(self.upload_torrent, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(
                                            seconds=3))
            self.update_config({
                "enabled": self._enabled,
                "is_paused": self._is_paused,
                "notify": self._notify,
                "onlyonce": self._onlyonce,
                "run_mothod": self._run_mothod,
                "monitor_type": self._monitor_type,
                "cron": self._cron,
                "torrent_dirs": self._torrent_dirs,
                "downloader": self._downloader,
            })

            # 启动服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @staticmethod
    def __check_file_permissions_rw(file_path: Path) -> bool:
        if file_path.exists():
            if os.access(file_path, os.R_OK) and os.access(file_path, os.W_OK):
                return True
        return False

    def __upload_torrent(self, torrent_path: str, save_path: str):
        """
        处理上传种子文件到下载器
        """
        service = self.service_info(self._downloader)
        downloader: Optional[Union[Qbittorrent, Transmission]] = service.instance if service else None
        if not downloader:
            return True, f"未找到下载器 {self._downloader}"

        success_cnt = 0
        failed_cnt = 0

        for file_path in SystemUtils.list_files(Path(torrent_path), ".torrent", recursive=False):
            if file_path.is_file():
                if not self.__check_file_permissions_rw(file_path):
                    return True, f"没有权限读写种子文件：{str(file_path)}"
                torrent_content = file_path.read_bytes()
                logger.info(f"找到种子文件：{str(file_path)}, 大小：{len(torrent_content)}")
                torrent = downloader.add_torrent(torrent_content, self._is_paused, save_path)
                if torrent:
                    success_cnt += 1
                    success_path = str(file_path.absolute().parent)+"/success/"
                    if not os.path.exists(success_path):
                        os.mkdir(success_path)
                    file_path.rename(success_path+file_path.name)
                else:
                    failed_cnt += 1
                    failed_path = str(file_path.absolute().parent)+"/failed/"
                    if not os.path.exists(failed_path):
                        os.mkdir(failed_path)
                    file_path.rename(failed_path+file_path.name)

        if success_cnt+failed_cnt == 0:
            return False, f"路径 {torrent_path} 没有找到种子文件"
        else:
            return False, f"""路径 {torrent_path} 有 {success_cnt+failed_cnt} 个种子：
  成功 {success_cnt} 个
  失败 {failed_cnt} 个"""

    def upload_torrent(self, torrent_dirs: list = None):
        """
        上传种子文件
        :return 有无错误，结果信息
        """
        logger.info("准备上传种子文件 ...")
        all_result = ""
        if torrent_dirs:
            _torrent_dirs = torrent_dirs
        else:
            _torrent_dirs = str(self._torrent_dirs).split("\n")
        try:
            for torrent_dir in _torrent_dirs:
                if torrent_dir.startswith("#"):
                    continue
                try:
                    file_path, save_path = torrent_dir.split(":")
                    error, result = self.__upload_torrent(file_path, save_path)
                    all_result += f"{result}\n"
                    if error:
                        logger.error("上传种子文件失败")
                        logger.error(all_result)
                        return True, all_result
                except ValueError:
                    logger.error("目录配置格式错误")
                    logger.error("一行一个，格式 '种子文件路径:保存路径'")
                    return True, "目录配置格式错误"

            logger.info("上传种子文件成功")
            logger.info(all_result)
        except Exception as e:
            logger.error("上传种子文件失败")
            logger.error(f"{e}")
            return True, f"{e}"
        return False, all_result

    def api_wrap(self, dir_pair: str = None) -> dict:
        if dir_pair:
            logger.info(f"处理指定的目录配置：{dir_pair}")
        error, result = self.upload_torrent([dir_pair] if dir_pair else None)
        return {
            "status": "FAILED" if error else "SUCCESS",
            "message": result
        }

    @eventmanager.register(EventType.PluginAction)
    def remote_sync_one(self, event: Event = None):
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "upload_torrent":
                return

            error, result = self.upload_torrent()
            if self._notify:
                if error:
                    msg_title="上传种子文件失败"
                else:
                    msg_title="上传种子文件成功"
                self.post_message(channel=event.event_data.get("channel"),
                                    title=msg_title,
                                    message=result,
                                    userid=event.event_data.get("user"))

    def service_info(self, name: str) -> Optional[ServiceInfo]:
        """
        服务信息
        """
        if not name:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        service = self.downloader_helper.get_service(name)
        if not service or not service.instance:
            logger.warning(f"获取下载器 {name} 实例失败，请检查配置")
            return None

        if service.instance.is_inactive():
            logger.warning(f"下载器 {name} 未连接，请检查配置")
            return None
        return service

    def get_state(self) -> bool:
        if self._enabled:
            if self._run_mothod == "monitor" and self._monitor_type:
                return True
            elif self._run_mothod == "cron" and self._cron:
                return True
        return False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/upload_torrent",
                "event": EventType.PluginAction,
                "desc": "种子下载",
                "category": "",
                "data": {
                    "action": "upload_torrent"
                }
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._run_mothod == "cron" and self._cron:
            return [{
                "id": "UploadTorrent",
                "name": "上传本地种子文件",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.upload_torrent,
                "kwargs": {}
            }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/upload_torrent",
            "endpoint": self.api_wrap,
            "methods": ["GET", "POST"],
            "summary": self.plugin_name,
            "description": self.plugin_desc,
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        downloader_options = [{"title": config.name, "value": config.name} for config in
                              self.downloader_helper.get_configs().values()]
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
                                            'model': 'is_paused',
                                            'label': '暂停种子',
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                            'model': 'downloader',
                                            'label': '下载器',
                                            'items': downloader_options
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
                                            'model': 'run_mothod',
                                            'label': '运行方式',
                                            'items': [
                                                {"title": "监控目录", "value": "monitor"},
                                                {"title": "定时运行", "value": "cron"},
                                            ]
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
                                            'model': 'monitor_type',
                                            'label': '监控方式',
                                            'items': [
                                                {"title": "性能模式", "value": "fast"},
                                                {"title": "兼容模式", "value": "compatibility"},
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
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '1 1 1 1 1'
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
                                            'rows': '3',
                                            'label': '目录配置',
                                            'placeholder': '一行一个，格式 "种子文件路径:保存路径"'
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
                                            'text': '种子文件路径是mp容器的路径，保存路径为下载器保存路径'
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
                                            'text': '一行开头写"#"代表注释这一行的配置，不递归查找子目录，上传失败的种子会移动到failed目录下，成功的会移动到success目录下'
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
                                            'type': 'warning',
                                            'variant': 'tonal',
                                            'text': '需要确保对种子文件和目录有读写权限'
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
            "is_paused": True,
            "notify": False,
            "onlyonce": False,
            "downloader": "qb",      
            "run_mothod": "cron",
            "monitor_type": "fast",
            "cron": "",
            "torrent_dirs": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        if self._observer:
            for observer in self._observer:
                try:
                    observer.stop()
                    observer.join()
                except Exception as e:
                    print(str(e))
            self._observer = []

        if self._scheduler:
            try:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
            except Exception as e:
                print(str(e))
            self._scheduler = None
