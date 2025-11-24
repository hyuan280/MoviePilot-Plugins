from datetime import datetime, timedelta
from threading import Event
from typing import Any, List, Dict, Tuple, Optional, Union

import os
import pytz
import random
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo

from plugins.torrentkeepalive.data import DataManager

class TorrentKeepAlive(_PluginBase):
    # 插件名称
    plugin_name = "做种保活"
    # 插件描述
    plugin_desc = "定时检查做种状态，重新开始未做种的种子"
    # 插件图标
    plugin_icon = "seed.png"
    # 插件版本
    plugin_version = "1.0.5"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "torrentkeepalive_"
    # 加载顺序
    plugin_order = 28
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _downloader_helper = DownloaderHelper()
    _data_manager: DataManager = None
    _downloader_style = {}

    # 开关
    _enabled = False
    _cron = None
    _clear = False
    _onlyonce = False
    _downloaders = None
    _notify = False
    _min_cnt = 1
    _max_number = 0
    # 退出事件
    _event = Event()

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._clear = config.get("clear")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._downloaders = config.get("downloaders")
            self._min_cnt = int(config.get("min_cnt", "1"))
            self._max_number = int(config.get("max_number", "0"))

        # 停止现有任务
        self.stop_service()

        # 获取数据目录
        data_path = self.get_data_path()
        # 确保目录存在
        if not os.path.exists(data_path):
            try:
                os.makedirs(data_path)
            except Exception as e:
                logger.error(f"创建数据目录失败: {str(e)}")

        # 给下载器随机配置背景色
        self._downloader_style = self._init_style(self._downloaders)

        # 初始化数据管理器
        self._data_manager = DataManager(data_path)
        if self._clear:
            self._data_manager.clear_all_data()
            self._clear = False
            config["clear"] = False
            self.update_config(config=config)

        # 启动定时任务 & 立即运行一次
        if self.get_state() or self._onlyonce:
            if not self.__validate_config():
                self._enabled = False
                self._onlyonce = False
                config["enabled"] = self._enabled
                config["onlyonce"] = self._onlyonce
                self.update_config(config=config)
                return

            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._onlyonce:
                logger.info(f"种子保活服务启动，立即运行一次")
                self._scheduler.add_job(self.keep_alive, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(
                                            seconds=3))
                self._onlyonce = False
                config["onlyonce"] = self._onlyonce
                self.update_config(config=config)
            # 启动服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @staticmethod
    def service_info(name: str) -> Optional[ServiceInfo]:
        """
        服务信息
        """
        if not name:
            logger.warning("尚未配置下载器，请检查配置")
            return None

        service = DownloaderHelper().get_service(name)
        if not service or not service.instance:
            logger.warning(f"获取下载器 {name} 实例失败，请检查配置")
            return None

        if service.instance.is_inactive():
            logger.warning(f"下载器 {name} 未连接，请检查配置")
            return None

        return service

    def get_state(self):
        return True if self._enabled \
                       and self._cron \
                       and self._downloaders else False

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

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
        if self.get_state():
            return [
                {
                    "id": "TorrentKeepAlive",
                    "name": self.plugin_name,
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.keep_alive,
                    "kwargs": {}
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        downloader_configs = DownloaderHelper().get_configs().values()
        downloader_options = []
        for config in downloader_configs:
            service = self.service_info(config.name)
            if self._downloader_helper.is_downloader("transmission", service=service):
                downloader_options.append(config.name)
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
                                            'model': 'clear',
                                            'label': '清除统计数据',
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
                                    'cols': 12
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
                                            'model': 'downloaders',
                                            'label': '下载器',
                                            'items': downloader_options
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'min_cnt',
                                            'label': '只显示保活多少次以上的',
                                            'placeholder': '1'
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'max_number',
                                            'label': '只显示多少条记录',
                                            'placeholder': '0'
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
            "notify": False,
            "clear": False,
            "onlyonce": False,
            "cron": "",
            "downloaders": "",
            "min_cnt": "1",
            "max_number": "0"
        }

    def get_page(self) -> List[dict]:
        cached_data = []
        torrent_data = self._data_manager.get_torrent_data()
        for downloader_name, data in torrent_data.items():
            for _,d in data.items():
                cached_data.append({**d, "downloader": downloader_name})

        cached_data.sort(key=lambda x:x.get("cnt"))

        if self._max_number > 0 and self._max_number <= len(cached_data):
            max_number = self._max_number
        else:
            max_number = len(cached_data)

        table_rows = []
        for i in range(max_number):
            item = cached_data[i]
            if item.get("cnt") >= self._min_cnt:
                table_rows.append({
                    "component": "tr",
                    "props": {
                        "style": self._downloader_style.get(item.get("downloader"))
                    },
                    "content": [
                        {"component": "td", "text": item.get("name")},
                        {"component": "td", "text": item.get("downloader")},
                        {"component": "td", "text": item.get("cnt")},
                        {
                            "component": "td",
                            "text": "等待保活" if item.get("status") == "waiting" else "已保活",
                            "props": {
                                "class": "text-error" if item.get("status") == "waiting" else "text-success"
                            },
                        },
                    ]
                })

        page_content = [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {'hover': True},
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {'class': 'text-start ps-4'},
                                                'text': f'名称（总数{len(cached_data)}）'
                                            },{
                                                'component': 'th',
                                                'props': {'class': 'text-start ps-4'},
                                                'text': '下载器'
                                            },{
                                                'component': 'th',
                                                'props': {'class': 'text-start ps-4'},
                                                'text': '保活计数'
                                            },{
                                                'component': 'th',
                                                'props': {'class': 'text-start ps-4'},
                                                'text': '状态'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': table_rows
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

        return page_content

    def __validate_config(self) -> bool:
        """
        校验配置
        """
        # 检查配置
        if not self._downloaders:
            logger.error(f"下载器未配置")
            self.systemmessage.put(f"下载器未配置", title=self.plugin_name)
            return False

        return True

    def __scheduler_restart_torrent(self, seconds):
        """
        添加重新做种任务
        """
        if self._scheduler is None:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            self._scheduler.start()

        # 延时区间20-120秒
        if seconds < 20:
            seconds = 20
        elif seconds > 120:
            seconds = 120

        logger.info(f"在 {seconds} 秒后重新开始种子")

        tz = pytz.timezone(settings.TZ)
        new_run_time = datetime.now(tz=tz) + timedelta(seconds=seconds)
        existing_job = self._scheduler.get_job("restart_torrent")
        if existing_job:
            existing_job.remove()

        self._scheduler.add_job(self.restart_torrent, 'date', id="restart_torrent",
                                run_date=new_run_time)

        logger.debug(f"添加任务：{self._scheduler.get_job("restart_torrent")}")

    def keep_alive(self):
        """
        开始种子保活
        """
        logger.info("开始种子保活任务 ...")
        restart_delay_s = 20

        if not self.__validate_config():
            return

        services = [self.service_info(downloader) for downloader in self._downloaders]
        for service in services:
            downloader: Optional[Union[Qbittorrent, Transmission]] = service.instance if service else None
            if not downloader:
                continue

            all_torrents = self._data_manager.get_torrent_data(service.name)
            keep_alive_torrents_ids = []
            torrents = downloader.get_completed_torrents()
            for torrent in torrents:
                if self._event.is_set():
                    logger.info(f"种子保活服务停止")
                    return
                for status in torrent.tracker_stats:
                    if status.next_announce_time == 0:
                        logger.info(f"{torrent}")
                        keep_alive_torrents_ids.append(torrent.id)

                        if all_torrents.get(torrent.hashString):
                            all_torrents[torrent.hashString]["cnt"] = all_torrents[torrent.hashString]["cnt"] + 1
                            all_torrents[torrent.hashString]["status"] = "waiting"
                        else:
                            all_torrents[torrent.hashString] = {
                                "name": torrent.name,
                                "id": torrent.id,
                                "cnt": 1,
                                "status": "waiting",
                            }
                        break

            if keep_alive_torrents_ids:
                torrent_cnt = len(keep_alive_torrents_ids)
                logger.info(f"下载器 {service.name} 未正常做种数：{torrent_cnt}")
            else:
                logger.info(f"下载器 {service.name} 做种正常")
                continue

            if not downloader.stop_torrents(keep_alive_torrents_ids):
                logger.error(f"下载器 {service.name} 停止种子失败，共 {torrent_cnt} 个种子")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【种子保活任务执行失败】",
                        text=f"下载器 {service.name} 停止种子失败，共 {torrent_cnt} 个种子"
                        )

            self._data_manager.update_torrent_data(service.name, all_torrents)
            restart_delay_s += torrent_cnt

        self.__scheduler_restart_torrent(restart_delay_s)

        logger.info("种子保活任务执行完成")

    def restart_torrent(self):
        """
        重新开始暂停的种子
        """
        logger.debug("开始重新做种...")
        message_text = ""
        torrent_data = self._data_manager.get_torrent_data()
        for downloader_name, data in torrent_data.items():
            ids = []
            service = self.service_info(downloader_name)
            if not service:
                continue
            downloader: Optional[Union[Qbittorrent, Transmission]] = service.instance if service else None
            if not downloader:
                continue
            for _, d in data.items():
                if d.get("status", "") == "waiting":
                    ids.append(d.get("id"))
                    d["status"] = "alive"
            if len(ids) == 0:
                continue
            if downloader.start_torrents(ids):
                logger.info(f"下载器 {service.name} 共保活 {len(ids)} 个种子")
                message_text += f"下载器 {service.name} 共保活 {len(ids)} 个种子\n"
            else:
                logger.error(f"下载器 {service.name} 保活失败，共 {len(ids)} 个种子")
                message_text += f"下载器 {service.name} 保活失败，共 {len(ids)} 个种子"

            self._data_manager.update_torrent_data(downloader_name, data)
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【种子保活任务执行完成】",
                text=message_text)

    def _init_style(self, color_name: list):
        style = {}
        bgStyles = {
            # 中性色系
            'light-gray': 'background-color: #f8f9fa;',
            'gray': 'background-color: #e9ecef;',
            'dark-gray': 'background-color: #dee2e6;',

            # 蓝色系
            'light-blue': 'background-color: #e3f2fd;',
            'blue': 'background-color: #bbdefb;',
            'dark-blue': 'background-color: #90caf9;',

            # 绿色系
            'light-green': 'background-color: #e8f5e8;',
            'green': 'background-color: #c8e6c9;',
            'dark-green': 'background-color: #a5d6a7;',

            # 红色系
            'light-red': 'background-color: #ffebee;',
            'red': 'background-color: #ffcdd2;',
            'dark-red': 'background-color: #ef9a9a;',

            # 黄色系
            'light-yellow': 'background-color: #fffde7;',
            'yellow': 'background-color: #fff9c4;',
            'dark-yellow': 'background-color: #fff59d;',

            # 橙色系
            'light-orange': 'background-color: #fff3e0;',
            'orange': 'background-color: #ffe0b2;',
            'dark-orange': 'background-color: #ffcc80;',

            # 紫色系
            'light-purple': 'background-color: #f3e5f5;',
            'purple': 'background-color: #e1bee7;',
            'dark-purple': 'background-color: #ce93d8;',
        }
        available_styles = bgStyles.copy()
        for name in color_name:
            if not available_styles:
                available_styles = bgStyles.copy()
            style_name = random.choice(list(available_styles.keys()))
            style[name] = available_styles.get(style_name)
            del available_styles[style_name]

        return style

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._event.set()
                    self._scheduler.shutdown()
                    self._event.clear()
                self._scheduler = None
        except Exception as e:
            print(str(e))
