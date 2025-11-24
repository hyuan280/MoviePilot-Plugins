"""
数据管理模块
"""
import os
import json
from typing import Dict, Any, List, Optional

from app.log import logger


class DataManager:
    """
    数据管理类
    """

    def __init__(self, data_path: str):
        """
        初始化数据管理
        :param data_path: 数据目录路径
        """
        self.data_path = data_path
        self.data_file = os.path.join(data_path, "torrent_alive.json")

    def load_data(self) -> Dict[str, Any]:
        """
        从文件加载数据
        :return: 数据字典
        """
        if not os.path.exists(self.data_file):
            return {}

        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"读取种子数据文件失败: {str(e)}")
            return {}

    def save_data(self, data: Dict[str, Any]) -> bool:
        """
        保存数据到文件
        :param data: 数据字典
        :return: 是否成功
        """
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)

            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"保存种子数据到文件失败: {str(e)}")
            return False

    def update_torrent_data(self, downloader_name: str, data: Dict[str, Any]) -> bool:
        """
        更新指定下载器的种子数据
        :param name: 下载器名称
        :param data: 种子数据
        :return: 是否成功
        """
        all_data = self.load_data()

        # 更新种子数据并添加时间戳
        all_data[downloader_name] = data

        return self.save_data(all_data)

    def get_torrent_data(self, downloader_name: Optional[str] = None) -> Dict[str, Any]:
        """
        获取种子数据
        :param name: 下载器名称，如果为None则返回所有种子数据
        :return: 种子数据
        """
        all_data = self.load_data()

        if downloader_name:
            return all_data.get(downloader_name, {})
        return all_data

    def clear_all_data(self) -> bool:
        """
        清空所有种子数据
        :return: 是否成功
        """
        try:
            if os.path.exists(self.data_file):
                # 直接清空为空字典
                return self.save_data({})
            return True
        except Exception as e:
            logger.error(f"清空种子数据失败: {str(e)}")
            return False
