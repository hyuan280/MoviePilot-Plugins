"""
Unit3dCE站点处理
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


class Unit3dCEHandler(_ISiteHandler):
    """
    Unit3dCE站点处理类
    """
    # 站点类型标识
    site_schema = SiteSchema.Unit3dCE

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
            "invite_url": site_url,
            "shop_url": site_url,
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

            result["invite_url"] = urljoin(site_url, f"users/{user_name}/invites")
            result["shop_url"] = urljoin(site_url, f"users/{user_name}/transactions/create")

            # --- 获取邀请数量和权限 ---
            try:
                user_url = urljoin(site_url, f"/users/{user_name}")
                logger.info(f"站点 {site_name} 正在从用户页获取邀请数量: {user_url}")
                user_response = session.get(user_url, timeout=(10, 30))
                user_response.raise_for_status()
                invite_result = self._parse_unit3dce_userspage(site_name, user_response.text)
                result["invite_status"]["can_invite"] = invite_result["can_invite"]
                result["invite_status"]["permanent_count"] = invite_result["permanent_count"]
                result["invite_status"]["temporary_count"] = 0 # 没有临时邀请
                logger.info(f"站点 {site_name} 从邀请页获取到邀请数量: 永久={invite_result['permanent_count']}, 临时=0")
            except Exception as e:
                logger.error(f"站点 {site_name} 从邀请页获取邀请数量失败: {str(e)}")

            if not result["invite_status"]["can_invite"]:
                try:
                    group_url = urljoin(site_url, "stats/groups/requirements")
                    logger.info(f"站点 {site_name} 访问用户组页面: {group_url}")
                    group_response = session.get(group_url, timeout=(10, 30))
                    group_response.raise_for_status()

                    invite_permission = self._check_unit3dce_invite_permission(site_name, user_level, group_response.text)
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
                logger.info(f"站点 {site_name} 访问魔力商店页面: {result["shop_url"]}")
                bonus_response = session.get(result["shop_url"], timeout=(10, 30))
                if bonus_response.status_code == 200:
                    bonus_data = self._parse_unit3dce_bonus_shop(site_name, bonus_response.text)
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

    def _check_unit3dce_invite_permission(self, site_name: str, user_level: str, html_content: str) -> Dict[str, Any]:
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

        levels_data = []

        try:
            # 初始化BeautifulSoup对象
            soup = BeautifulSoup(html_content, 'html.parser')
            level_rows = soup.find_all('tr')
            for row in level_rows:
                level_data = {}
                level_span = row.find('span')
                if level_span:
                    # 提取等级名称（去掉图标和空白）
                    level_text = level_span.get_text(strip=True)
                    # 使用正则提取纯等级名称
                    level_match = re.search(r'([A-Za-z\s]+)$', level_text)
                    if level_match:
                        level_data['level'] = level_match.group(1).strip()
                    else:
                        level_data['level'] = level_text

                    # 解析权限表格
                    perks_table = row.find('table', class_='stats__perks-table')
                    if perks_table:
                        level_data['perks'] = self._parse_perks_table(perks_table)

                if level_data:  # 只添加有数据的等级
                    levels_data.append(level_data)

        except Exception as e:
            logger.error(f"站点 {site_name} 解析用户组权限失败: {str(e)}")
            return result

        for level_data in levels_data:
            if level_data.get("level", "") == user_level:
                perks =  level_data.get("perks", [])
                if perks and '寄出邀请' in perks:
                    logger.debug(f"站点 {site_name} 用户等级可以发送邀请")
                    result['can_invite'] = True
                    result['reason'] = "用户等级可以发送邀请"
                    break

        if not result['can_invite']:
            for level_data in levels_data:
                perks =  level_data.get("perks", [])
                if perks and '寄出邀请' in perks:
                    invite_level = level_data.get("level", "")
                    logger.debug(f"站点 {site_name} {invite_level} 或以上等级才可以发送邀请")
                    result['can_invite'] = False
                    result['reason'] = f"{invite_level} 或以上等级才可以发送邀请"
                    break

        return result

    @staticmethod
    def _parse_perks_table(table):
        """
        解析权限表格
        """
        perks = []
        tbody = table.find('tbody')
        if not tbody:
            return perks
        
        rows = tbody.find_all('tr')
        for row in rows:
            cell = row.find('td')
            if cell:
                perk_text = cell.get_text(strip=True)
                # 提取纯文本权限（去掉图标文本）
                clean_perk = re.sub(r'^[^A-Za-z\u4e00-\u9fff]*', '', perk_text)
                if clean_perk:
                    perks.append(clean_perk)
        
        return perks

    def _parse_unit3dce_bonus_shop(self, site_name: str, html_content: str) -> Dict[str, Any]:
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
            bonus_li = soup.find('li', class_='ratio-bar__points')
            if bonus_li:
                a_tag = bonus_li.find('a')
                if a_tag:
                    # 获取所有文本内容
                    text_content = a_tag.get_text()
                    # 提取所有数字并合并
                    numbers = re.findall(r'\d+', text_content)
                    if numbers:
                        result["bonus"] = float(''.join(numbers))

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

    def _parse_unit3dce_userspage(self, site_name: str, html_content: str) -> Dict[str, Any]:
        """
        解析站点用户页，获取邀请权限和数量
        :param site_name: 站点名称
        :param html_content: HTML内容
        :return: 邀请数量
        """
        result = {
            "can_invite": False,
            "permanent_count": 0,
            "temporary_count": 0
        }

        try:
            # 初始化BeautifulSoup对象
            soup = BeautifulSoup(html_content, 'html.parser')
            
            for dt in soup.find_all('dt'):
                if '可以邀请' in dt.get_text(strip=True):
                    dd = dt.find_next_sibling('dd')
                    if dd:
                        i_tag = dd.find('i')
                        if i_tag and 'text-green' in i_tag.get('class', []):
                            result["can_invite"] = True

                elif '邀请' in dt.get_text(strip=True):
                    dd = dt.find_next_sibling('dd')
                    if dd:
                        try:
                            result["permanent_count"] = int(dd.get_text(strip=True))
                            logger.info(f"站点 {site_name} 从用户页面匹配到邀请数量: 永久={result['permanent_count']}")
                        except (ValueError, TypeError):
                            logger.warning(f"站点 {site_name} 无法将用户页面中的邀请数量转换为整数: {dd.get_text(strip=True)}")
                    else:
                        logger.warning(f"站点 {site_name} 无法从用户页面解析到邀请数量")
        except Exception as e:
            logger.error(f"站点 {site_name} 解析用户页面的邀请数量失败: {str(e)}")

        return result
