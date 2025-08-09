import re
import os
import time
import json
import pytz

from lxml import etree
from urllib.parse import urljoin
from datetime import datetime, timedelta
from threading import Event
from typing import Any, List, Dict, Tuple
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from concurrent.futures import ThreadPoolExecutor, wait

from app.core.config import settings
from app.helper.cloudflare import under_challenge
from app.helper.sites import SitesHelper
from app.db.site_oper import SiteOper
from app.log import logger
from app.plugins import _PluginBase
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.schemas import NotificationType


class AutoClaim(_PluginBase):
    # 插件名称
    plugin_name = "自动认领"
    # 插件描述
    plugin_desc = "定时认领做种的种子"
    # 插件图标
    plugin_icon = "seed.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "autoclaim_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _scheduler = None
    _thread_pool = None
    _futures = []

    # 开关
    _enabled = False
    _cron = None
    _onlyonce = False
    _sites = None
    _notify = False
    _queues = 1
    _size = 0
    _seedtime = 0
    # 退出事件
    _event = Event()
    _site_infos = []
    _claim_result = {}

    def init_plugin(self, config: dict = None):
        self._site_infos = []
        self._claim_result = {}

        # 读取配置
        if config:
            self._enabled = config.get("enabled")
            self._onlyonce = config.get("onlyonce")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._sites = config.get("sites", [])
            self._queues = config.get("queues", 1)
            self._size = config.get("size", 0)
            self._seedtime = config.get("seedtime", 0)

        if isinstance(self._queues, str):
            self._queues = int(self._queues)
        if isinstance(self._size, str):
            self._size = int(self._size)
        if isinstance(self._seedtime, str):
            self._seedtime = int(self._seedtime)

        # 停止现有任务
        self.stop_service()

        if not self.get_state():
            return

        if self._sites:
            site_id_to_public_status = {site.get("id"): site.get("public") for site in SitesHelper().get_indexers()}
            self._sites = [
                site_id for site_id in self._sites
                if site_id in site_id_to_public_status and not site_id_to_public_status[site_id]
            ]
            config['sites'] = self._sites
            self.update_config(config=config)

        for siteid in self._sites:
            siteinfo = SiteOper().get(siteid)
            if siteinfo:
                self._site_infos.append(siteinfo)
        if self._site_infos:
            logger.info(f"即将认领站点 {', '.join(site.name for site in self._site_infos)} 的种子")

        # 启动定时任务 & 立即运行一次
        if self.get_state() or self._onlyonce:
            if not self.__validate_config():
                self._enabled = False
                self._onlyonce = False
                config["enabled"] = self._enabled
                config["onlyonce"] = self._onlyonce
                self.update_config(config=config)
                return

            # 线程池
            self._thread_pool = ThreadPoolExecutor(max_workers=self._queues)
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)

            if self._onlyonce:
                logger.info(f"认领种子服务启动，立即运行一次")
                self._scheduler.add_job(self.auto_claim, 'date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(
                                            seconds=3))
                self._onlyonce = False
                config["onlyonce"] = self._onlyonce
                self.update_config(config=config)
            # 启动服务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_state(self):
        return True if self._enabled \
                       and self._cron \
                       and self._sites else False

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
                    "id": "AutoClaim",
                    "name": self.plugin_name,
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.auto_claim,
                    "kwargs": {}
                }
            ]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        cpu_options = [{"title": n, "value": n} for n in range(1, os.cpu_count() + 1)]
        site_options = []
        for site in SitesHelper().get_indexers():
            if site.get("schema", "").startswith("Nexus"):
                site_options.append({"title": site.get("name"), "value": site.get("id")})
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
                                    'cols': 12,
                                    'md': 4,
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {

                                            'model': 'queues',
                                            'label': '同时处理站点数目',
                                            'items': cpu_options
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'size',
                                            'label': '认领的最小种子大小(MB)',
                                            'placeholder': '0 不限制'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4,
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'seedtime',
                                            'label': '认领的最小做种时间(小时)',
                                            'placeholder': '0 不限制'
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
                                            'model': 'sites',
                                            'label': '认领种子的站点',
                                            'items': site_options
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
            "sites": "",
            "queues": 1,
            "size": 0,
            "seedtime": 0,
        }

    def get_page(self) -> List[dict]:
        pass

    def __validate_config(self) -> bool:
        """
        校验配置
        """
        # 检查配置
        if not self._sites:
            logger.error(f"站点未配置")
            self.systemmessage.put(f"站点未配置", title=self.plugin_name)
            return False

        return True

    def _get_user_torrent_seeding_html(self, site, userid, page: int = 0):
        """
        获取用户做种页和页数
        """
        html_text = ""
        url = urljoin(site.url, f"/getusertorrentlistajax.php?page={page}&userid={userid}&type=seeding")
        req_headers = { "User-Agent": f"{site.ua}" }

        logger.debug(f"请求网页：{url}")
        res = RequestUtils(cookies=site.cookie,
                            timeout=60,
                            proxies=site.proxy,
                            headers=req_headers).get_res(url=url)

        if res is not None and res.status_code in (200, 500, 403):
            # 如果cloudflare 有防护，尝试使用浏览器仿真
            if under_challenge(res.text):
                logger.warn(
                    f"{self._site_name} 检测到Cloudflare，请更新Cookie和UA")
                return None, ""

            html_text = RequestUtils.get_decoded_html_content(res,
                                settings.ENCODING_DETECTION_PERFORMANCE_MODE,
                                settings.ENCODING_DETECTION_MIN_CONFIDENCE)

        else:
            logger.error(f"无法访问站点页面：{url}")
            return None, ""

        html_text = self._prepare_html_text(html_text)
        html = etree.HTML(html_text)
        try:
            if html is None:
                logger.error(f"空页面：{url}")
                return None, ""
            page_emls = html.xpath("//*[@class='nexus-pagination']")
            if page_emls:
                page_eml = page_emls[0]
            else:
                logger.debug(f"没有下一页，最终页：{page}")
                return None, html_text
            page_links = page_eml.xpath("./a[contains(@href, 'page=')]/@href")
            if page_links:
                page_numbers = []
                for link in page_links:
                    match = re.search(r'page=(\d+)', link)
                    if match:
                        page_numbers.append(int(match.group(1)))

                pages = max(page_numbers) + 1 if page_numbers else 1
                logger.debug(f"总页数 {pages}({pages+1})，当前页 {page+1}")
                if pages > page+1:
                    return page+1, html_text
            return None, html_text
        except Exception as e:
            logger.error(f"解析网页错误：{e}")
            return None, ""

    def _task_auto_claim(self, site, userid):
        page, html_text = self._get_user_torrent_seeding_html(site, userid)
        self._addClaim(site, page, html_text)

        while page:
            if self._event.is_set():
                logger.info(f"认领种子服务停止")
                return

            page, html_text = self._get_user_torrent_seeding_html(site, userid, page)
            self._addClaim(site, page, html_text)

    def auto_claim(self):
        """
        开始种子保活
        """
        logger.info("开始认领种子任务 ...")

        if not self.__validate_config():
            return

        self._futures = []
        self._claim_result = {}
        for site in self._site_infos:
            if self._event.is_set():
                logger.info(f"认领种子服务停止")
                return

            userdatas = SiteOper().get_userdata_by_domain(site.domain)
            if not userdatas:
                continue
            else:
                userdata = userdatas[-1]
            logger.info(f"站点 {site.name} 用户id {userdata.userid}")

            # 提交任务到线程池
            future = self._thread_pool.submit(self._task_auto_claim, site, userdata.userid)
            if future:
                self._futures.append(future)
            else:
                logger.error(f"任务提交到线程失败，站点：{site.name}")
                continue

        if self._futures:
            wait(self._futures)

        message_text = ""
        for k, v in self._claim_result.items():
            message_text += f"站点 {k}：\n"
            message_text += f" 认领    成功：{v.get('success')}\n"
            message_text += f" 认领    跳过：{v.get('skip')}\n"
            message_text += f" 认领人数上限：{v.get('excessive')}\n"
            message_text += f" 认领    失败：{v.get('failed')}\n\n"

        logger.info(f"{message_text}")

        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【认领种子执行完成】",
                text=message_text
                )

    @staticmethod
    def _parse_torrent_info(html):
        torrent_info = {}
        def time_span_to_seconds(time_str):
            # 匹配天数、小时、分钟、秒
            pattern = r"(?:(\d+)\s*[天日dD])?\s*(?:(\d+):)?(\d+):(\d+)"
            match = re.match(pattern, time_str)
            if not match:
                raise ValueError(f"无法解析时间格式: {time_str}")

            days = int(match.group(1) or 0)
            hours = int(match.group(2) or 0)
            minutes = int(match.group(3))
            seconds = int(match.group(4))

            total_seconds = (days * 24 * 3600) + (hours * 3600) + (minutes * 60) + seconds
            return total_seconds
        time_index = 0
        size_index = 0

        headers = html.xpath("//tr[1]/td")
        for index, header in enumerate(headers, start=1):
            text = header.xpath("string()").strip()
            if "做种时间" in text or "Time" in text:
                time_index = index
                continue
            if header.xpath(".//img[contains(@class,'size')]"):
                size_index = index
                continue

        if time_index == 0 or size_index == 0:
            return torrent_info

        trs = html.xpath("//tr")
        for tr in trs[1:]:
            torrent_id = 0
            torrent_links = tr.xpath(".//a[contains(@href, 'id=')]/@href")
            if torrent_links:
                match = re.search(r'id=(\d+)', torrent_links[0])
                if match:
                    torrent_id = (int(match.group(1)))
            if torrent_id == 0:
                continue
            size_text = tr.xpath(f"./td[{size_index}]/text()")
            time_text = tr.xpath(f"./td[{time_index}]/text()")
            if size_text and time_text:
                try:
                    size = StringUtils.num_filesize(''.join(size_text).strip()) # 字节
                    time = time_span_to_seconds(''.join(time_text).strip()) # 秒
                except Exception as e:
                    logger.error(f"解析种子信息错误：{e}")
                    continue

                torrent_info[torrent_id] = { "time": int(time/(60*60)), "size": int(size/(1024*1024)) }

        return torrent_info

    def _addClaim(self, site, page, html_text):
        cur_err_cnt = 0 # 解析错误
        success_cnt = 0 # 成功认领
        skip_cnt = 0 # 条件不满足跳过认领
        excessive_cnt = 0 # 认领人数过多
        failed_cnt = 0 #认领失败

        if self._claim_result.get(site.name):
            old_result = self._claim_result.get(site.name)
            success_cnt = old_result.get("success")
            skip_cnt = old_result.get("skip")
            excessive_cnt = old_result.get("excessive")
            failed_cnt = old_result.get("failed")

        url = urljoin(site.url, "/ajax.php")
        logger.debug(f"认领网址：{url}")
        req_headers = {
            #"Content-Type": "application/json",
            "User-Agent": f"{site.ua}"
        }
        html = etree.HTML(html_text)
        try:
            torrent_infos = self._parse_torrent_info(html)
        except Exception as e:
            logger.error(f"解析种子信息页面错误：{e}")
            return

        elements = html.xpath("//button[@data-action='addClaim' and not(contains(@style, 'display: none'))]")
        logger.info(f"站点 {site.name} 页面 {page} 有种子 {len(elements)} 个可以认领")
        for e in elements:
            if self._event.is_set():
                logger.info(f"认领种子服务停止")
                return
            if cur_err_cnt > 5:
                return

            torrent_id = e.get('data-torrent_id')
            if not torrent_id:
                failed_cnt += 1
                continue

            torrent_info = torrent_infos.get(torrent_id)
            if torrent_info:
                if self._seedtime and torrent_info.get("time") < self._seedtime:
                    logger.debug(f"站点 {site.name} 种子 {torrent_id}: 做种时间不足（{torrent_info.get("time")}）小时")
                    skip_cnt += 1
                    continue
                if self._size and torrent_info.get("size") < self._size:
                    logger.debug(f"站点 {site.name} 种子 {torrent_id}: 大小不够（{torrent_info.get("size")}）MB")
                    skip_cnt += 1
                    continue

            data = {
                "action": "addClaim",
                "params[torrent_id]": torrent_id,
            }

            time.sleep(0.5)
            logger.info(f"认领站点 {site.name} 种子 {torrent_id}")
            res = RequestUtils(cookies=site.cookie,
                                timeout=60,
                                proxies=site.proxy,
                                headers=req_headers).post_res(url=url, data=data)

            if res is not None and res.status_code in (200, 500, 403):
                try:
                    result = res.json()
                except (json.JSONDecodeError, ValueError) as e:
                    logger.error(f"站点 {site.name} 认领种子 {torrent_id} 结果获取失败：{e}")
                    cur_err_cnt += 1
                    continue

                if result.get('ret') == 0:
                    success_cnt += 1
                elif result.get('msg') == "认领达到人数上限":
                    excessive_cnt += 1
                else:
                    failed_cnt += 1
            else:
                cur_err_cnt += 1

        self._claim_result[site.name] = {
            "success": success_cnt,
            "skip": skip_cnt,
            "excessive": excessive_cnt,
            "failed": failed_cnt,
        }

    @staticmethod
    def _prepare_html_text(html_text):
        """
        处理掉HTML中的干扰部分
        """
        return re.sub(r"#\d+", "", re.sub(r"\d+px", "", html_text))

    def stop_service(self):
        """
        退出插件
        """
        self._event.set()
        if self._thread_pool:
            try:
                self._thread_pool.shutdown(wait=True)
            except Exception as e:
                logger.error(f"线程池关闭失败：{e}")
            self._thread_pool = None
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"停止调度器失败：{e}")
        self._event.clear()
