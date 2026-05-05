import random
import time
import pickle
import traceback

from threading import RLock
from pathlib import Path
from typing import Optional

from app.core.meta import MetaBase
from app.core.config import settings
from app.log import logger

lock = RLock()
CACHE_EXPIRE_TIMESTAMP_STR = "cache_expire_timestamp"
CONF = settings.CONF
if isinstance(CONF, dict):
    EXPIRE_TIMESTAMP = CONF["meta"]
else:
    EXPIRE_TIMESTAMP = CONF.meta

class JAVCache():
    """
    jav缓存数据
    {
        "title": '',
        "year": '',
        "error": 0,
        "detail_link": '',
    }
    """
    _meta_data: dict = {}
    # 缓存文件路径
    _meta_path: Path = None
    # 缓存过期
    _cache_expire: bool = True

    def __init__(self, name):
        self._meta_path = settings.TEMP_PATH / f"__jav_{name}_cache__"
        self._meta_data = self.__load(self._meta_path)

    def clear(self):
        """
        清空所有缓存
        """
        with lock:
            self._meta_data = {}

    @staticmethod
    def __get_key(meta: MetaBase) -> str:
        """
        获取缓存KEY
        """
        return f"{meta.cn_name}-{meta.year}-{meta.begin_season}"

    def get(self, meta: MetaBase):
        """
        根据KEY值获取缓存值
        """
        key = self.__get_key(meta)
        with lock:
            info: dict = self._meta_data.get(key)
            if info:
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire or int(time.time()) < expire:
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                    self._meta_data[key] = info
                elif expire and self._cache_expire:
                    self.delete(key)
            return info or {}

    def delete(self, key: str) -> dict:
        """
        删除缓存信息
        @param key: 缓存key
        @return: 被删除的缓存内容
        """
        with lock:
            return self._meta_data.pop(key, {})

    @staticmethod
    def __load(path: Path) -> dict:
        """
        从文件中加载缓存
        """
        try:
            if path.exists():
                with open(path, 'rb') as f:
                    data = pickle.load(f)
                return data
            return {}
        except Exception as e:
            logger.error(f"加载缓存失败: {str(e)} - {traceback.format_exc()}")
            return {}

    def update(self, meta: MetaBase, data) -> None:
        """
        新增或更新缓存条目
        """
        with lock:
            if data:
                # 缓存标题
                cache_title = data.title
                # 缓存年份
                cache_year = data.year
                # 海报

                self._meta_data[self.__get_key(meta)] = {
                        "year": cache_year,
                        "title": cache_title,
                        "error": 0,
                        "data": data,
                        CACHE_EXPIRE_TIMESTAMP_STR: int(time.time()) + EXPIRE_TIMESTAMP
                    }
            else:
                cache_title = meta.cn_name if meta.cn_name else meta.en_name
                if self._meta_data.get(self.__get_key(meta)):
                    error = self._meta_data.get(self.__get_key(meta)).get('error') + 1
                else:
                    error = 1
                self._meta_data[self.__get_key(meta)] = {
                        "year": None,
                        "title": cache_title,
                        "error": error,
                        "data": None,
                        CACHE_EXPIRE_TIMESTAMP_STR: int(time.time()) + EXPIRE_TIMESTAMP
                    }

    def save(self, force: Optional[bool] = False) -> None:
        """
        保存缓存数据到文件
        """

        meta_data = self.__load(self._meta_path)
        new_meta_data = {k: v for k, v in self._meta_data.items() if v.get("data")}

        if not force \
                and not self._random_sample(new_meta_data) \
                and meta_data.keys() == new_meta_data.keys():
            return

        with open(self._meta_path, 'wb') as f:
            pickle.dump(new_meta_data, f, pickle.HIGHEST_PROTOCOL) # noqa

    def _random_sample(self, new_meta_data: dict) -> bool:
        """
        采样分析是否需要保存
        """
        ret = False
        if len(new_meta_data) < 25:
            keys = list(new_meta_data.keys())
            for k in keys:
                info = new_meta_data.get(k)
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire:
                    ret = True
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                elif int(time.time()) >= expire:
                    ret = True
                    if self._cache_expire:
                        new_meta_data.pop(k)
        else:
            count = 0
            keys = random.sample(sorted(new_meta_data.keys()), 25)
            for k in keys:
                info = new_meta_data.get(k)
                expire = info.get(CACHE_EXPIRE_TIMESTAMP_STR)
                if not expire:
                    ret = True
                    info[CACHE_EXPIRE_TIMESTAMP_STR] = int(time.time()) + EXPIRE_TIMESTAMP
                elif int(time.time()) >= expire:
                    ret = True
                    if self._cache_expire:
                        new_meta_data.pop(k)
                        count += 1
            if count >= 5:
                ret |= self._random_sample(new_meta_data)
        return ret

    def get_title(self, key: str) -> Optional[str]:
        """
        获取缓存的标题
        """
        cache_media_info = self._meta_data.get(key)
        if not cache_media_info:
            return None
        return cache_media_info.get("title")

    def set_title(self, key: str, cn_title: str) -> None:
        """
        重新设置缓存标题
        """
        cache_media_info = self._meta_data.get(key)
        if not cache_media_info:
            return
        self._meta_data[key]['title'] = cn_title


class JAVScraper():
    pass

