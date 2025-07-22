from typing import List, Tuple, Dict, Any

from app.log import logger
from app.plugins import _PluginBase
from app.db.systemconfig_oper import SystemConfigOper


class FlareSolverr(_PluginBase):
    # 插件名称
    plugin_name = "FlareSolverr代理"
    # 插件描述
    plugin_desc = "给系统增加FlareSolverr代理，可以使用FlareSolverr代理绕过CF"
    # 插件图标
    plugin_icon = "https://lsky.hy2zy.fun/uploads/1/2025-07-22/FlareSolverr.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "hyuan280"
    # 作者主页
    author_url = "https://github.com/hyuan280"
    # 插件配置项ID前缀
    plugin_config_prefix = "flaresolverr_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # preivate property
    _enabled = False
    _flaresolverr_url = ""
    _flaresolverr_timeout = 0

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get('enabled')
            self._flaresolverr_url = config.get('flaresolverr_url')
            self._flaresolverr_timeout = config.get('flaresolverr_timeout')

        if self.get_state():
            logger.info(f"配置FlareSolverr")
            system_config_oper = SystemConfigOper()
            system_config_oper.set("flaresolverr_url", self._flaresolverr_url)
            system_config_oper.set("flaresolverr_timeout", self._flaresolverr_timeout)

    def get_state(self) -> bool:
        return self._enabled

    def get_service(self) -> List[Dict[str, Any]]:
        pass

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            }
                        ],
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
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'flaresolverr_url',
                                            'label': 'FlareSolverr URL',
                                            'placeholder': 'http://localhost:8191/v1'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'flaresolverr_timeout',
                                            'label': 'FlareSolverr连接超时时间',
                                            'placeholder': '超时时间（秒）'
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
            "flaresolverr_url": "http://localhost:8191/v1",
            "flaresolverr_timeout": 180
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        logger.info(f"删除FlareSolverr")
        system_config_oper = SystemConfigOper()
        system_config_oper.delete("flaresolverr_url")
        system_config_oper.delete("flaresolverr_timeout")
