import time
import re
from typing import Tuple

from ruamel.yaml import CommentedMap
from lxml import etree

from app.core.config import settings
from app.core.event import EventManager, eventmanager, Event
from app.schemas.types import EventType
from app.log import logger
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.db.site_oper import SiteOper


class Unit3dCE(_ISiteSigninHandler):
    """
    Unit3dCE系列站点签到
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_urls = ["https://animelovers.club", "https://bitporn.eu"]
    is_refresh = False

    @classmethod
    def match(cls, url: str) -> bool:
        """
        根据站点Url判断是否匹配当前站点签到类，大部分情况使用默认实现即可
        :param url: 站点Url
        :return: 是否匹配，如匹配则会调用该类的signin方法
        """
        for site_url in cls.site_urls:
            if StringUtils.url_equal(site_url, url):
                return True
        return False

    def fail_site_refresh(self, site_id):
        self.is_refresh = True
        logger.info("更新站点的Cookie和UA，稍后重新签到")
        eventmanager.send_event(EventType.PluginAction,
                                {
                                    "site_id": site_id,
                                    "action": "site_refresh"
                                })
        time.sleep(120)

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """
        logger.debug(f"开始签到...")
        site = site_info.get("name")
        site_id = site_info.get("id")
        site_url = site_info.get("url")
        site_data = SiteOper().get(site_id)
        site_cookie = site_data.cookie
        ua = site_data.ua
        proxies = settings.PROXY if site_data.proxy else None

        # 获取主页html
        html_res = RequestUtils(cookies=site_cookie,
                                ua=ua,
                                proxies=proxies
                                ).get_res(url=site_url)
        if not html_res or html_res.status_code != 200:
            if not self.is_refresh:
                self.fail_site_refresh(site_id)
                return self.signin(site_info)
            logger.error(f"{site} 签到失败，请检查站点连通性{site_url}")
            return False, '签到失败，请检查站点连通性'

        if f"{site_url}/login" in html_res.text:
            logger.error(f"{site} 签到失败，Cookie已失效")
            return False, '签到失败，Cookie已失效'

        html = etree.HTML(html_res.text)
        if not html:
            logger.error(f"{site} 签到失败，解析主页错误")
            return False, '签到失败，解析主页错误'

        event_urls = html.xpath('//a[contains(@href, "/events/")]/@href')
        if event_urls:
            event_urls = sorted(set(event_urls))
        else:
            logger.error(f"{site} 签到失败，没有找到事件入口")
            return False, '签到失败，没有找到事件入口'

        signin_msg = '签到成功'
        for event_url in event_urls:
            site_tip = site + ' ' + event_url.replace(site_url, '') + ':'
            # 获取签到页
            html_res = RequestUtils(cookies=site_cookie,
                                    ua=ua,
                                    proxies=proxies
                                    ).get_res(url=event_url)
            if not html_res or html_res.status_code != 200:
                if not self.is_refresh:
                    self.fail_site_refresh(site_id)
                    return self.signin(site_info)
                logger.error(f"{site_tip} 签到失败，请检查站点连通性")
                return False, '签到失败，请检查站点连通性'

            html = etree.HTML(html_res.text)
            if not html:
                logger.error(f"{site_tip} 签到失败，解析签到页错误")
                return False, '签到失败，解析签到页错误'

            sign_form = html.xpath('//form[contains(@class, "form") and contains(@action, "/claims")]')
            if sign_form:
                sign_form = sign_form[0]
            else:
                logger.info(f"{site_tip} 今日已签到")
                self.__getbonus(site_tip, html_res.text)
                signin_msg = '今日已签到'
                continue

            token = sign_form.xpath('.//input[@name="_token"]/@value')
            if token:
                token = token[0]
            else:
                logger.error(f"{site_tip} 签到失败，签到页没有token的值")
                return False, '签到失败，签到页没有token的值'

            html_res = RequestUtils(cookies=site_cookie,
                                    ua=ua,
                                    proxies=proxies
                                    ).post_res(url=f"{event_url}/claims", data={"_token": token})
            if not html_res or html_res.status_code != 200:
                if not self.is_refresh:
                    self.fail_site_refresh(site_id)
                    return self.signin(site_info)
                logger.error(f"{site_tip} 签到失败，Cookie已失效")
                return False, '签到失败，Cookie已失效'

            self.__getbonus(site_tip, html_res.text)
            signin_msg = '签到成功'

        return True, signin_msg

    def __getbonus(self, tip, html_text: str):
        html = etree.HTML(html_text)
        if not html:
            return
        last_brons = 0
        brons_info = ""
        events = html.xpath('//i[contains(@class, "events__prize-message")]')
        if events:
            for event in events:
                data = event.xpath("string(.)").strip()
                data = re.sub(r'[\n\s]+', " ", data)
                if data == 'Check back later!':
                    break
                brons = data.split()[0]
                if brons.isdigit():
                    last_brons = int(brons)
                    brons_info = data

        if last_brons > 0:
            logger.info(f"{tip} 签到获取 {brons_info}")
