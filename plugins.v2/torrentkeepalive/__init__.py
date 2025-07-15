from datetime import datetime, timedelta
from threading import Event
from typing import Any, List, Dict, Tuple, Optional, Union

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo


class TorrentKeepAlive(_PluginBase):
    # 插件名称
    plugin_name = "做种保活"
    # 插件描述
    plugin_desc = "定时检查做种状态，重新开始未做种的种子"
    # 插件图标
    plugin_icon = "seed.png"
    # 插件版本
    plugin_version = "1.0.0"
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

    # 开关
    _enabled = False
    _cron = None
    _onlyonce = False
    _downloaders = None
    _notify = False
    # 退出事件
    _event = Event()
    # 待保活种子清单
    _keep_alive_torrents = {}

    def init_plugin(self, config: dict = None):
        self._keep_alive_torrents = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._downloaders = config.get("downloaders")

        # 停止现有任务
        self.stop_service()

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
                                    'md': 4
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
                                    'md': 4
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
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": False,
            "onlyonce": False,
            "cron": "",
            "downloaders": "",
        }

    def get_page(self) -> List[dict]:
        pass

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

    def keep_alive(self):
        """
        开始种子保活
        """
        logger.info("开始种子保活任务 ...")

        if not self.__validate_config():
            return

        services = [self.service_info(downloader) for downloader in self._downloaders]
        for service in services:
            downloader: Optional[Union[Qbittorrent, Transmission]] = service.instance if service else None
            if not downloader:
                return

            keep_alive_torrents = self._keep_alive_torrents.get(service.name)
            if keep_alive_torrents:
                logger.info(f"下载器 {service.name} 还有种子{len(keep_alive_torrents)}个等待重新开始，下一次任务再检查")
                return

            keep_alive_torrents = []
            torrents = downloader.get_completed_torrents()
            for torrent in torrents:
                for status in torrent.tracker_stats:
                    if status.next_announce_time == 0:
                        logger.info(f"{torrent}")
                        keep_alive_torrents.append(torrent.id)
                        break

            if keep_alive_torrents:
                torrent_cnt = len(keep_alive_torrents)
                logger.info(f"下载器 {service.name} 未正常做种数：{torrent_cnt}")
            else:
                logger.info(f"下载器 {service.name} 做种正常")
                return

            if not downloader.stop_torrents(keep_alive_torrents):
                logger.error(f"下载器 {service.name} 停止种子失败，共 {torrent_cnt} 个种子")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【种子保活任务执行失败】",
                        text=f"下载器 {service.name} 停止种子失败，共 {torrent_cnt} 个种子"
                        )

            self._keep_alive_torrents[service.name] = keep_alive_torrents

            logger.info(f"下载器 {service.name} 在 {torrent_cnt} 秒后重新开始种子")
            self._scheduler.add_job(self.restart_torrent, 'date',
                                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(
                                        seconds=torrent_cnt))

        logger.info("种子保活任务执行完成")

    def restart_torrent(self):
        """
        重新开始暂停的种子
        """
        for name, ids in self._keep_alive_torrents.items():
            service = self.service_info(name)
            if not service:
                continue
            downloader: Optional[Union[Qbittorrent, Transmission]] = service.instance if service else None
            if not downloader:
                continue
            if not ids:
                continue
            if downloader.start_torrents(ids):
                logger.info(f"下载器 {service.name} 共保活 {len(ids)} 个种子")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【种子保活任务执行完成】",
                        text=f"下载器 {service.name} 共保活 {len(ids)} 个种子"
                        )
            else:
                logger.error(f"下载器 {service.name} 保活失败，共 {len(ids)} 个种子")
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【种子保活任务执行失败】",
                        text=f"下载器 {service.name} 保活失败，共 {len(ids)} 个种子"
                        )

        self._keep_alive_torrents = {}

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
