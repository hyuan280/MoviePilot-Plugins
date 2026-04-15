import datetime
import threading
from typing import List, Tuple, Dict, Any, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.helper.downloader import DownloaderHelper
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.plugins import _PluginBase
from app.schemas import ServiceInfo


class qBAutoClassify(_PluginBase):
    # 插件名称
    plugin_name = "qB下载器自动分类"
    # 插件描述
    plugin_desc = "qB下载器自动根据现有保存路径和现存分类自动分类并自动管理"
    # 插件图标
    plugin_icon = "Qbittorrent_A.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "qbautoclassify_"
    # 加载顺序
    plugin_order = 2
    # 可使用的用户级别
    auth_level = 1

    # 退出事件
    _event = threading.Event()
    # 私有属性
    _scheduler = None
    _downloader_helper = DownloaderHelper()

    #配置开关
    _enabled = False
    _onlyonce = False
    _auto_manage = True
    _clear_tags = True
    _interval = "计划任务"
    _interval_cron = "5 4 * * *"
    _interval_time = 6
    _interval_unit = "小时"
    _downloaders = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._auto_manage = config.get("auto_manage")
            self._clear_tags = config.get("clear_tags")
            self._interval = config.get("interval") or "计划任务"
            self._interval_cron = config.get("interval_cron") or "5 4 * * *"
            self._interval_time = config.get("interval_time") or 6
            self._interval_unit = config.get("interval_unit") or "小时"
            self._downloaders = config.get("downloaders")

        if isinstance(self._interval_time, str):
            try:
                self._interval_time = int(self._interval_time)
            except Exception as e:
                self._interval_time = 6
            config.update({"interval_time": self._interval_time})

        # 停止现有任务
        self.stop_service()

        if self._onlyonce:
            # 创建定时任务控制器
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            # 执行一次, 关闭onlyonce
            self._onlyonce = False
            config.update({"onlyonce": self._onlyonce})
            self.update_config(config)
            # 添加 下载器自动分类 任务
            self._scheduler.add_job(func=self._auto_classify, trigger='date',
                                    run_date=datetime.datetime.now(
                                        tz=pytz.timezone(settings.TZ)) + datetime.timedelta(seconds=3)
                                    )

            if self._scheduler and self._scheduler.get_jobs():
                # 启动服务
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

    def get_state(self) -> bool:
        return self._enabled

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
        # 初始化公共服务列表
        tasks = []
        if self.get_state():
            if self._interval == "固定间隔":
                if self._interval_unit == "小时":
                    kwargs = {"hours": self._interval_time}
                else:
                    if self._interval_time < 5:
                        self._interval_time = 5
                        logger.error(f"启动定时服务: 最小不少于5分钟!")
                    kwargs = {"minutes": self._interval_time}
                tasks.append({
                    "id": "qBAutoClassify",
                    "name": "qB下载器自动分类",
                    "trigger": "interval",
                    "func": self._auto_classify,
                    "kwargs": kwargs
                })
            elif self._interval == "计划任务" and self._interval_cron:
                tasks.append({
                        "id": "qBAutoClassify",
                        "name": "qB下载器自动分类",
                        "trigger": CronTrigger.from_crontab(self._interval_cron),
                        "func": self._auto_classify,
                        "kwargs": {}
                    })
        return tasks

    def _auto_classify(self):
        """
        自动分类任务
        """

        services: ServiceInfo = [self.service_info(downloader) for downloader in self._downloaders]
        for service in services:
            if self._event.is_set():
                logger.info(f"下载器自动分类服务停止")
                return

            downloader: Qbittorrent = service.instance if service else None
            if not downloader:
                continue

            if downloader.qbc is None:
                logger.error(f"下载器 {service.name} 未连接")
                continue

            # categories = {
            #   '电影': {
            #       'name': '电影',
            #       'savePath': '/media/Seeds/movie'
            # }
            downloader_categories = downloader.qbc.torrent_categories.categories
            if not downloader_categories:
                logger.error(f"下载器 {service.name} 没有配置分类")
                continue

            categories = {}
            for _,v in downloader_categories.items():
                if v.get("savePath"):
                    categories[v.get("savePath")] = v.get("name")
            logger.debug(f"categories={categories}")

            all_torrents = downloader.get_completed_torrents()
            logger.info(f"下载器 {service.name} 有种子 {len(all_torrents)} 个")

            class_cnt = 0
            for torrent in all_torrents:
                if self._event.is_set():
                    logger.info(f"下载器自动分类服务停止")
                    return

                info = torrent.info
                if not info.category and categories.get(info.save_path):
                    logger.info(f"下载器 {service.name} 分类种子 {torrent.info.name} ==> {categories.get(info.save_path)}")
                    class_cnt += 1
                    try:
                        # 设置分类
                        torrent.setCategory(categories.get(info.save_path))
                        # 设置自动管理
                        if self._auto_manage:
                            torrent.setAutoManagement(True)
                        # 删除tags
                        if self._clear_tags:
                            if info.tags:
                                torrent.removeTags(info.tags)
                    except Exception as e:
                        logger.error(f"下载器 {service.name} 分类种子异常：{e}")
                        return
            logger.info(f"下载器 {service.name} 共分类种子 {class_cnt} 个")

        logger.info(f"下载器自动分类服务执行完成")

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        downloader_configs = DownloaderHelper().get_configs().values()
        downloader_options = []
        for config in downloader_configs:
            service = self.service_info(config.name)
            if self._downloader_helper.is_downloader("qbittorrent", service=service):
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
                            },{
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
                                            'label': '立即运行一次'
                                        }
                                    }
                                ]
                            },{
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'auto_manage',
                                            'label': '自动管理'
                                        }
                                    }
                                ]
                            },{
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear_tags',
                                            'label': '清理分类后的标签'
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
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'interval',
                                            'label': '定时任务',
                                            'items': [
                                                {'title': '禁用', 'value': '禁用'},
                                                {'title': '计划任务', 'value': '计划任务'},
                                                {'title': '固定间隔', 'value': '固定间隔'}
                                            ]
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval_cron',
                                            'label': '计划任务设置',
                                            'placeholder': '5 4 * * *'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                    'md': 3,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval_time',
                                            'label': '固定间隔设置, 间隔每',
                                            'placeholder': '6'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 6,
                                    'md': 3,
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'model': 'interval_unit',
                                            'label': '单位',
                                            'items': [
                                                {'title': '小时', 'value': '小时'},
                                                {'title': '分钟', 'value': '分钟'}
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
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '定时任务：支持两种定时方式，主要针对辅种转移等种子自动分类。如没有对应的需求建议切换为禁用。'
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
            "auto_manage": True,
            "clear_tags": True,
            "interval": "计划任务",
            "interval_cron": "5 4 * * *",
            "interval_time": "6",
            "interval_unit": "小时",
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        停止服务
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
            logger.error(str(e))
