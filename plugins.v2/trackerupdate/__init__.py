from datetime import datetime, timedelta
from typing import List, Tuple, Dict, Any, Union, Optional
from threading import Event
import pytz

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.log import logger
from app.modules.qbittorrent import Qbittorrent
from app.modules.transmission import Transmission
from app.helper.downloader import DownloaderHelper
from app.plugins import _PluginBase
from app.schemas import NotificationType, ServiceInfo


class TrackerUpdate(_PluginBase):
    # 插件名称
    plugin_name = "Tracker更新"
    # 插件描述
    plugin_desc = "批量修改种子tracker"
    # 插件图标
    plugin_icon = "trackereditor_A.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "trackerupdate_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _scheduler = None
    _downloader_helper = DownloaderHelper()

    # 开关
    _enabled: bool = False
    _onlyonce: bool = False
    _cron: Optional[str] = None
    _downloaders: list = []
    _tracker_config: str = None
    _notify: bool = False

    # 退出事件
    _event = Event()

    def init_plugin(self, config: dict = None):
        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._downloaders = config.get("downloaders", [])
            self._tracker_config = config.get("tracker_config")
            self._notify = config.get("notify")

        # 停止现有任务
        self.stop_service()

        # 启动定时任务 & 立即运行一次
        if self.get_state() or self._onlyonce:

            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._onlyonce:
                logger.info(f"Tracker服务启动，立即运行一次")
                self._scheduler.add_job(self.task, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(
                                            seconds=3))
                self._onlyonce = False
                self.__update_config()

            # 启动服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    @staticmethod
    def update_trackers(tracker_list: list, tracker_edits: list):
        """
        根据编辑操作更新tracker列表

        规则：
        1. modify: 替换匹配的tracker
        2. add: 为匹配的tracker添加多个新tracker
        3. delete: 删除匹配的tracker，但不能删除最后一个

        Args:
            tracker_list: 原始tracker列表
            tracker_edits: 编辑操作列表

        Returns:
            (是否被编辑, 新tracker列表, 被删除的tracker列表)
        """
        if not tracker_list or not tracker_edits:
            return False, list(tracker_list), []

        # 创建副本
        new_urls = list(tracker_list)
        old_urls = []
        is_edited = False

        # 先收集所有要执行的操作
        operations = []

        for tracker_edit in tracker_edits:
            edit_type = tracker_edit.get("type")
            edit_src = tracker_edit.get("old", "")
            edit_dests = tracker_edit.get("new", [])

            if not edit_src:
                continue

            # 对每个tracker检查是否匹配
            for tracker in tracker_list:
                if edit_src in tracker:
                    operations.append({
                        "type": edit_type,
                        "original": tracker,
                        "src": edit_src,
                        "dests": edit_dests if isinstance(edit_dests, list) else [edit_dests]
                    })

        # 按顺序执行操作
        for op in operations:
            op_type = op["type"]
            original = op["original"]
            src = op["src"]
            dests = op["dests"]

            if op_type == "modify" and dests:
                # modify 操作：替换第一个匹配的dest
                if original in new_urls:
                    is_edited = True
                    new_urls.remove(original)
                    old_urls.append(original)
                    new_urls.append(original.replace(src, dests[0]))

            elif op_type == "add":
                # add 操作：保留原tracker，并添加新的
                if original in new_urls:
                    for dest in dests:
                        new_tracker = original.replace(src, dest)
                        if new_tracker not in new_urls:  # 避免重复
                            is_edited = True
                            new_urls.append(new_tracker)

            elif op_type == "delete":
                # delete 操作：不能删除最后一个
                if original in new_urls and len(new_urls) > 1:
                    is_edited = True
                    new_urls.remove(original)
                    old_urls.append(original)

        return is_edited, new_urls, old_urls

    def task(self):
        logger.info(f"开始执行Tracker更新")

        services = [self.service_info(downloader) for downloader in self._downloaders]
        tracker_edits = []
        for tracker_config in self._tracker_config.split("\n"):
            if tracker_config.count('|') == 1:
                parts = tracker_config.split('|')
                if len(parts) == 2:
                    old_part, new_part = parts[0].strip(), parts[1].strip()
                    tracker_edits.append({
                        "type": "modify",
                        "old": old_part,
                        "new": new_part,
                    })
                else:
                    logger.error(f"修改配置格式错误: {tracker_config}")
            elif ';' in tracker_config:
                confs = [c.strip() for c in tracker_config.split(';') if c.strip()]
                if len(confs) >= 2:
                    tracker_edits.append({
                        "type": "add",
                        "old": confs[0],
                        "new": confs[1:],
                    })
                else:
                    logger.error(f"添加配置格式错误: {tracker_config}")
            else:
                tracker_edits.append({
                    "type": "delete",
                    "old": tracker_config,
                })

        if not tracker_edits:
            logger.info("没有配置tracker更新")
            if self._notify:
                self.post_message(
                    mtype=NotificationType.SiteMessage,
                    title="【Tracker更新任务执行完成】",
                    text="没有配置tracker更新")
        logger.debug(f"tracker更新配置：{tracker_edits}")

        message_text = "\n"

        for service in services:
            torrent_total_cnt = 0
            torrent_update_cnt = 0
            downloader: Optional[Union[Qbittorrent, Transmission]] = service.instance if service else None
            if not downloader:
                continue

            logger.info(f"下载器 {service.name} 更新tracker ...")
            torrents, error = downloader.get_torrents()
            torrent_total_cnt = len(torrents)
            if error:
                message_text += f"下载器 {service.name} 获取种子异常：{error}\n"
                logger.info(message_text)
                continue

            if self._downloader_helper.is_downloader("qbittorrent", service=service):
                try:
                    for torrent in torrents:
                        if self._event.is_set():
                            logger.info(f"更新tracker服务停止")
                            return
                        skip_trackers = ['** [DHT] **', '** [PeX] **', '** [LSD] **']
                        tracker_list = list(filter(lambda x: x not in skip_trackers, [tracker.url for tracker in torrent.trackers]))
                        edited, new_trackers, old_trackers = self.update_trackers(tracker_list, tracker_edits)
                        if edited:
                            torrent_update_cnt += 1
                            if old_trackers:
                                torrent.remove_trackers(urls=old_trackers)
                            if new_trackers:
                                torrent.add_trackers(urls=new_trackers)

                except Exception as e:
                    message_text += f"下载器 {service.name} 执行tracker修改出错，中止任务\n"
                    logger.error(message_text)
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【Tracker更新任务执行中止】",
                            text=message_text)
                    return
            elif self._downloader_helper.is_downloader("transmission", service=service):
                for torrent in torrents:
                    if self._event.is_set():
                        logger.info(f"更新tracker服务停止")
                        return
                    tracker_list = torrent.tracker_list[0]
                    if not isinstance(tracker_list, list):
                        tracker_list = torrent.tracker_list
                    edited, new_trackers, _ = self.update_trackers(tracker_list, tracker_edits)
                    if edited and new_trackers:
                        torrent_update_cnt += 1
                        update_result = downloader.update_tracker(hash_string=torrent.hashString, tracker_list=[new_trackers])
                        if not update_result:
                            message_text += f"下载器 {service.name} 执行tracker修改出错，中止任务\n"
                            logger.error(message_text)
                            if self._notify:
                                self.post_message(
                                    mtype=NotificationType.SiteMessage,
                                    title="【Tracker更新任务执行中止】",
                                    text=message_text)
                            return

            else:
                message_text += f"下载器 {service.name} 类型不是qb/tr!\n"
                logger.info(message_text)
                continue
            message_text += f"下载器 {service.name}\n\t总的种子数: {torrent_total_cnt}, 已修改种子数: {torrent_update_cnt}\n"

        logger.info("Tracker更新任务执行完成")
        logger.info(message_text)
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【Tracker更新任务执行完成】",
                text=message_text)

    def __update_config(self):
        self.update_config({
            "enabled": self._enabled,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "downloaders": self._downloaders,
            "tracker_config": self._tracker_config,
            "notify": self._notify
        })

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        if self.get_state():
            return [
                {
                    "id": "TrackerUpdate",
                    "name": self.plugin_name,
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.task,
                    "kwargs": {}
                }]

        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        downloader_options = [config.name for config in DownloaderHelper().get_configs().values()]
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
                                    'md': 6
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
                                    'md': 6
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
                            }]
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
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '发送通知',
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
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'tracker_config',
                                            'label': 'tracker更新配置',
                                            'rows': 6,
                                            'placeholder': '每一行一个配置，中间以|或者;分隔\n'
                                                           '(替换tracker)待替换文本|替换的文本\n'
                                                           '(增加tracker)待替换文本;替换的文本;替换的文本...'
                                                           '(删除tracker)待替换文本',
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
                                            'text': '对下载器中所有符合代替换文本的tacker进行增加、替换、删除' + '\n' +
                                                    '现有tracker: https://baidu.com/announce.php?passkey=xxxx' + '\n' +
                                                    '增加qq.com： baidu.com;qq.com' + '\n' +
                                                    '替换成qq.com:  baidu.com|qq.com' + '\n' +
                                                    '删除baidu.com： baidu.com',
                                            'style': 'white-space: pre-line;'
                                        }
                                    },
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '强烈建议自己先添加一个tracker测试更新是否符合预期，程序是否正常运行',
                                            'style': 'white-space: pre-line;'
                                        }
                                    },
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '支持qb，tr仅支持4.0以上版本' + '\n',
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
            "onlyonce": False,
            "downloaders": [],
            "tracker_config":"",
            "enabled": False,
            "cron": "",
            "notify": True
        }

    def get_page(self) -> List[dict]:
        pass

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
        return True if self._enabled \
                       and self._cron \
                       and self._downloaders else False

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
