import re
import traceback
from typing import Tuple

from ruamel.yaml import CommentedMap
from urllib.parse import urljoin

from app.core.config import settings
from app.plugins.autosignin.sites import _ISiteSigninHandler
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.log import logger
from app.utils.http import RequestUtils
from app.utils.string import StringUtils
from app.utils.site import SiteUtils


class FlareSolverr(_ISiteSigninHandler):
    """
    通过FlareSolverr签到
    """

    # 匹配的站点Url，每一个实现类都需要设置为自己的站点Url
    site_urls = ["https://pt.0ff.cc/", "https://share.ilolicon.com/", "https://www.hddolby.com/"]

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

    def signin(self, site_info: CommentedMap) -> Tuple[bool, str]:
        """
        执行签到操作
        :param site_info: 站点信息，含有站点Url、站点Cookie、UA等信息
        :return: 签到结果信息
        """

        system_config_oper = SystemConfigOper()
        flare_url = system_config_oper.get("flaresolverr_url")
        timeout = system_config_oper.get("flaresolverr_timeout")

        # 判断登录状态
        if not site_info:
            return False, ""
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None
        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法签到")
            return False, ""
        # 模拟登录
        try:
            # 访问链接
            checkin_url = site_url
            if site_url.find("attendance.php") == -1:
                # 拼登签到地址
                checkin_url = urljoin(site_url, "attendance.php")
            logger.info(f"开始站点签到：{site}，地址：{checkin_url}...")
            if render:
                page_source = PlaywrightHelper().get_page_source(url=checkin_url,
                                                                 cookies=site_cookie,
                                                                 ua=ua,
                                                                 proxies=proxy_server)
                if not SiteUtils.is_logged_in(page_source):
                    if under_challenge(page_source):
                        return False, f"无法通过Cloudflare！"
                    return False, f"仿真登录失败，Cookie已失效！"
                else:
                    # 判断是否已签到
                    if re.search(r'已签|签到已得', page_source, re.IGNORECASE) \
                            or SiteUtils.is_checkin(page_source):
                        return True, f"签到成功"
                    return True, "仿真签到成功"
            elif 'cf_clearance' in site_cookie and flare_url != None:
                logger.info(f"有CF墙，使用FlareSolver绕过")
                custom_cookies = self._parse_cookies(site_cookie)
                res = self._flare_solverr_cookie(flare_url, timeout, checkin_url, custom_cookies)
                # 判断登录状态
                if res and res.get('status') in [200, 500, 403]:
                    if not SiteUtils.is_logged_in(res.get('response')):
                        if under_challenge(res.get('response')):
                            msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                        elif res.status_code == 200:
                            msg = "Cookie已失效"
                        else:
                            msg = f"状态码：{res.get('status')}"
                        logger.warn(f"{site} 签到失败，{msg}")
                        return False, f"签到失败，{msg}！"
                    else:
                        logger.info(f"{site} 签到成功")
                        return True, f"签到成功"
                elif res is not None:
                    logger.warn(f"{site} 签到失败，状态码：{res.get('status')}")
                    return False, f"签到失败，状态码：{res.get('status')}！"
                else:
                    logger.warn(f"{site} 签到失败，无法打开网站")
                    return False, f"签到失败，无法打开网站！"

            else:
                res = RequestUtils(cookies=site_cookie,
                                   ua=ua,
                                   proxies=proxies
                                   ).get_res(url=checkin_url)
                if not res and site_url != checkin_url:
                    logger.info(f"开始站点模拟登录：{site}，地址：{site_url}...")
                    res = RequestUtils(cookies=site_cookie,
                                       ua=ua,
                                       proxies=proxies
                                       ).get_res(url=site_url)
                # 判断登录状态
                if res and res.status_code in [200, 500, 403]:
                    if not SiteUtils.is_logged_in(res.text):
                        if under_challenge(res.text):
                            msg = "站点被Cloudflare防护，请打开站点浏览器仿真"
                        elif res.status_code == 200:
                            msg = "Cookie已失效"
                        else:
                            msg = f"状态码：{res.status_code}"
                        logger.warn(f"{site} 签到失败，{msg}")
                        return False, f"签到失败，{msg}！"
                    else:
                        logger.info(f"{site} 签到成功")
                        return True, f"签到成功"
                elif res is not None:
                    logger.warn(f"{site} 签到失败，状态码：{res.status_code}")
                    return False, f"签到失败，状态码：{res.status_code}！"
                else:
                    logger.warn(f"{site} 签到失败，无法打开网站")
                    return False, f"签到失败，无法打开网站！"
        except Exception as e:
            logger.warn("%s 签到失败：%s" % (site, str(e)))
            traceback.print_exc()
            return False, f"签到失败：{str(e)}！"

    @staticmethod
    def _flare_solverr_cookie(flare_url: str, timeout: int, url: str, custom_cookies: list) -> Tuple[None, dict]:
        import requests

        flare_payload = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": timeout*1000,
            "cookies": custom_cookies
        }
        flare_response = requests.post(flare_url, json=flare_payload)
        flare_data = flare_response.json()
        if flare_data.get("status") != "ok":
            print("FlareSolverr 请求失败:", flare_data.get("message"))
            return None
        return flare_data.get('solution')

    @staticmethod
    def _parse_cookies(cookie_str: str) -> list:
        cookies = []
        for item in cookie_str.split(";"):
            item = item.strip()
            if "=" in item:
                name, value = item.split("=", 1)
                cookies.append({"name": name, "value": value})
        return cookies
