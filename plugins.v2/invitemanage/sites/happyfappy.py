"""
HappyFappy站点处理
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


class HappyFappyHandler(_ISiteHandler):
    """
    HappyFappy站点处理类
    """
    # 站点类型标识
    site_schema = SiteSchema.HappyFappy

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
            "shop_url": urljoin(site_url, "bonus.php"),
            "invite_status": {
                "can_invite": False,
                "reason": "站点开放注册，不需要邀请",
                "permanent_count": 0,
                "temporary_count": 0,
                "bonus": 0,  # 魔力值
                "permanent_invite_price": 0,  # 永久邀请价格
                "temporary_invite_price": 0   # 临时邀请价格
            },
            "invitees": []
        }

        userdata = None
        latest_datas = SiteOper().get_userdata_latest()
        if latest_datas:
            for data in latest_datas:
                if data and data.domain == StringUtils.get_url_domain(site_url):
                    userdata = data
        if userdata:
            result["invite_status"]["bonus"] = userdata.bonus

        return result
