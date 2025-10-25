"""
Unit3站点处理
"""
import re
import json
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app.modules.indexer.parser import SiteSchema
from app.utils.string import StringUtils
from app.db.site_oper import SiteOper
from app.log import logger


from plugins.invitemanage.sites import _ISiteHandler


class Unit3dHandler(_ISiteHandler):
    """
    Unit3站点处理类
    """
    # 站点类型标识
    site_schema = SiteSchema.Unit3d

    def parse_invite_page(self, site_info: Dict[str, Any], session: requests.Session) -> Dict[str, Any]:
        """
        解析站点邀请页面
        :param site_info: 站点信息
        :param session: 已配置好的请求会话
        :return: 解析结果
        """
        site_name = site_info.get("name", "")
        site_url = site_info.get("url", "")
        site_id = site_info.get("id")

        logger.info(f"开始解析站点 {site_name} 邀请页面，站点ID: {site_id}, URL: {site_url}")

        result = {
            "invite_status": {
                "can_invite": False,
                "reason": "",
                "permanent_count": 0,
                "temporary_count": 0,
                "bonus": 0,  # 魔力值
                "permanent_invite_price": 0,  # 永久邀请价格
                "temporary_invite_price": 0   # 临时邀请价格
            },
            "invitees": []
        }

        try:
            # 获取用户ID
            userdata = None
            latest_datas = SiteOper().get_userdata_latest()
            if latest_datas:
                for data in latest_datas:
                    if data and data.domain == StringUtils.get_url_domain(site_url):
                        userdata = data
            if userdata:
                user_level = userdata.user_level
                user_name = userdata.username

            if not user_level:
                logger.error(f"站点 {site_name} 无法获取用户等级")
                result["invite_status"]["reason"] = "无法获取用户等级，请检查站点Cookie是否有效"
                return result

            if not user_name:
                logger.error(f"站点 {site_name} 无法获取用户名")
                result["invite_status"]["reason"] = "无法获取用户名，请检查站点Cookie是否有效"
                return result
            # --- 获取邀请数量和权限 ---
            try:
                invites_url = urljoin(site_url, f"invites/{user_name}")
                logger.info(f"站点 {site_name} 正在从邀请页获取邀请数量: {invites_url}")
                invites_response = session.get(invites_url, timeout=(10, 30))
                invites_response.raise_for_status()
                invite_counts = self._parse_unit3d_invitespage(site_name, invites_response.text)
                result["invite_status"]["permanent_count"] = invite_counts["permanent_count"]
                result["invite_status"]["temporary_count"] = 0 # 没有临时邀请
                logger.info(f"站点 {site_name} 从邀请页获取到邀请数量: 永久={invite_counts['permanent_count']}, 临时=0")
            except Exception as e:
                logger.error(f"站点 {site_name} 从邀请页获取邀请数量失败: {str(e)}")

            try:
                page_url = urljoin(site_url, "pages")
                logger.info(f"站点 {site_name} 访问帮助页面: {page_url}")
                page_response = session.get(page_url, timeout=(10, 30))
                page_response.raise_for_status()

                group_url = self._parse_unit3d_group_url(site_name, page_response.text)
                if group_url == "":
                    result["invite_status"]["reason"] = "无法获取用户组URL"
                else:
                    logger.info(f"站点 {site_name} 访问用户组页面: {group_url}")
                    group_response = session.get(group_url, timeout=(10, 30))
                    group_response.raise_for_status()
                    invite_permission = self._check_unit3d_invite_permission(site_name, user_level, group_response.text)
                    result["invite_status"]["can_invite"] = invite_permission["can_invite"]
                    if result["invite_status"]["can_invite"]:
                        if result["invite_status"]["permanent_count"] > 0:
                            result["invite_status"]["reason"] = f"可用邀请数: 永久={result['invite_status']['permanent_count']}"
                        else:
                            result["invite_status"]["reason"] = invite_permission["reason"]
                    else:
                        result["invite_status"]["reason"] = invite_permission["reason"]
            except Exception as e:
                 logger.error(f"站点 {site_name} 获取或检查邀请权限页面失败: {str(e)}")
                 result["invite_status"]["reason"] = f"检查邀请权限失败: {str(e)}"
            # --- 邀请数量和权限获取结束 ---

            # --- 获取魔力值和邀请价格 ---
            try:
                bonus_url = urljoin(site_url, f"users/{user_name}/bonus/transactions/create")
                logger.info(f"站点 {site_name} 访问魔力商店页面: {bonus_url}")
                bonus_response = session.get(bonus_url, timeout=(10, 30))
                if bonus_response.status_code == 200:
                    bonus_data = self._parse_unit3d_bonus_shop(site_name, bonus_response.text)
                    result["invite_status"]["bonus"] = bonus_data["bonus"]
                    result["invite_status"]["permanent_invite_price"] = bonus_data["permanent_invite_price"]
                    result["invite_status"]["temporary_invite_price"] = 0

            except Exception as e:
                logger.warning(f"站点 {site_name} 解析魔力值商店失败: {str(e)}")
            # --- 魔力值解析结束 ---

            if result["invitees"]:
                 logger.info(f"站点 {site_name} 共解析到 {len(result['invitees'])} 个邀请成员")

            return result

        except Exception as e:
            logger.error(f"解析站点 {site_name} 邀请页面时发生严重错误: {str(e)}")
            result["invite_status"]["reason"] = f"解析邀请页面失败: {str(e)}"
            return result

    def _check_unit3d_invite_permission(self, site_name: str, user_level: str, html_content: str) -> Dict[str, Any]:
        """
        检查站点邀请权限
        :param site_name: 站点名称
        :param html_content: HTML内容
        :return: 邀请权限
        """
        result = {
            "can_invite": False,
            "reason": ""
        }

        is_admin_group = False
        invite_level = ""
        invite_permissions = []

        try:
            # 初始化BeautifulSoup对象
            soup = BeautifulSoup(html_content, 'html.parser')
            for p_tag in soup.find_all('p'):
                if not is_admin_group:
                    p_text = p_tag.get_text(strip=True)
                    if p_text and '管理组' in p_text:
                        is_admin_group = True

                span_tag = p_tag.find('span', class_='badge-user')
                if span_tag:
                    # 提取等级名称
                    level_name = span_tag.get_text(strip=True)
                    # 获取权限信息
                    permissions = []
                    code_tags = p_tag.find_all('code')
                    for code_tag in code_tags:
                        permissions.append(code_tag.get_text(strip=True))

                    if user_level == level_name:
                        invite_permissions = permissions
                    elif invite_level == "":
                        if permissions and '访问邀请区' in permissions:
                            invite_level = level_name

            if invite_permissions and '访问邀请区' in invite_permissions:
                logger.debug(f"站点 {site_name} 用户等级可以发送邀请")
                result['can_invite'] = True
                result['reason'] = "用户等级可以发送邀请"
            else:
                logger.debug(f"站点 {site_name} {invite_level} 或以上等级才可以发送邀请")
                result['can_invite'] = False
                result['reason'] = f"{invite_level} 或以上等级才可以发送邀请"

        except Exception as e:
            logger.error(f"站点 {site_name} 解析用户组权限失败: {str(e)}")
        return result

    def _parse_unit3d_bonus_shop(self, site_name: str, html_content: str) -> Dict[str, Any]:
        """
        解析站点魔力值商店页面
        :param site_name: 站点名称
        :param html_content: HTML内容
        :return: 魔力值和邀请价格信息
        """
        result = {
            "bonus": 0,                  # 用户当前魔力值
            "permanent_invite_price": 0, # 永久邀请价格
            "temporary_invite_price": 0  # 站点无临时邀请
        }

        # 初始化BeautifulSoup对象
        soup = BeautifulSoup(html_content, 'html.parser')

        try:
            # 1. 查找当前魔力值
            dd_tag = soup.select('dt:contains("总魔力点数") + dd')
            if dd_tag:
                dd_text = dd_tag[0].get_text(strip=True)
                result["bonus"] = float(dd_text.replace(',', ''))

            # 2. 查找邀请价格
            dd_tag = soup.select('td:contains("1 Invite") + td')
            if dd_tag:
                dd_text = dd_tag[0].get_text(strip=True)
                result["permanent_invite_price"] = float(dd_text.replace(',', ''))
                logger.info(f"站点 {site_name} 永久邀请价格: {dd_text}")

        except Exception as e:
            logger.error(f"解析站点 {site_name} 魔力值商店失败: {str(e)}")

        logger.debug(f"result={result}")
        return result

    def _parse_unit3d_group_url(self, site_name: str, html_content: str) -> str:
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            # 查找所有li标签中的a标签
            for li in soup.find_all('li'):
                a_tag = li.find('a')
                if a_tag:
                    if '用户等级' in a_tag.get_text() or 'Groups' in a_tag.get_text():
                        return a_tag.get('href')
        except Exception as e:
            logger.error(f"站点 {site_name} 解析用户组页面失败: {str(e)}")

        return ""

    def _parse_unit3d_invitespage(self, site_name: str, html_content: str) -> Dict[str, Any]:
        """
        解析站点邀请页，获取邀请数量
        :param site_name: 站点名称
        :param html_content: HTML内容
        :return: 邀请数量
        """
        result = {
            "permanent_count": 0,
            "temporary_count": 0
        }

        try:
            # 初始化BeautifulSoup对象
            soup = BeautifulSoup(html_content, 'html.parser')

            # 查找邀请li
            li_element = soup.find('li', class_='ratio-bar__buffer')
            if li_element:
                invite_tag = li_element.select_one('a')
                if invite_tag:
                    invite_text = invite_tag.get_text(strip=True)
                    invite_match = re.search(r'[\s&nbsp;]*(\d+)', invite_text)
                    if invite_match:
                        try:
                            result["permanent_count"] = int(invite_match.group(1))
                            logger.info(f"站点 {site_name} 从邀请页面匹配到邀请数量: 永久={result['permanent_count']}")
                        except (ValueError, TypeError):
                            logger.warning(f"站点 {site_name} 无法将邀请页面中的邀请数量转换为整数: {invite_match.group(1)}")
                    else:
                        logger.warning(f"站点 {site_name} 邀请页面未能提取邀请数量: {invite_text}")

        except Exception as e:
            logger.error(f"站点 {site_name} 解析邀请页面的邀请数量失败: {str(e)}")

        return result
