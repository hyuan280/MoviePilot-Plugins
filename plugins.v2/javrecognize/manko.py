import re
import requests
import json
from typing import Optional, Tuple, Union

from app.core.meta import MetaBase
from app.core.context import MediaInfo
from app.modules import _ModuleBase
from app.log import logger
from app.schemas.types import MediaType, ModuleType, MediaRecognizeType
from app.utils.http import RequestUtils, AsyncRequestUtils

from .common import JAVCache, JAVScraper

#
# Manko 识别api
#
class MankoApi():
    _base_url = "https://manko.fun"
    _search_url = "https://healertanker.com/swx/movie/search?keyword=%s&size=24&page=1"
    _detail_url = "https://healertanker.com/swx/movie/detail/%s"
    _actors_url = "https://healertanker.com/swx/movie/actors/%s?size=6&page=1"

    _tags_zh = {
                "3D": "3D",
                "3p, 4p": "3便士，4便士",
                "4HR+": "4小时以上",
                "4K": "4K",
                "Action": "行动",
                "Adult Movie": "成人电影",
                "Amateur": "业余",
                "Anus": "肛门",
                "BBW": "大码美女",
                "Beautiful foot": "美丽的脚",
                "Beautiful Girl Movie": "美女电影",
                "Best, Omnibus": "最佳，综合",
                "Big cock": "大鸡巴",
                "Big Tits": "大胸",
                "Blow": "吹",
                "Breast Milk": "母乳",
                "Breasts": "乳房",
                "Butt": "屁股",
                "Chroma Key": "色键",
                "Classic": "经典的",
                "Close Up": "特写",
                "Co-act": "合作",
                "Coprophagy": "食粪癖",
                "Cowgirl": "女牛仔",
                "Creampie": "奶油派",
                "Cum": "合",
                "Cunnilingus": "口交",
                "dating": "约会",
                "Debut Production": "首部作品",
                "Deep Throating": "深喉",
                "Defecation": "排便",
                "Digital Mosaic": "数字马赛克",
                "Dirty Words": "脏话",
                "Documentary": "记录",
                "Doggy style": "狗式",
                "Drama": "戏剧",
                "Facesitting": "脸坐",
                "Facials": "面部护理",
                "Fan Appreciation": "粉丝答谢",
                "FFM": "FFM",
                "Fighting Action": "战斗行动",
                "Finger Fuck": "手指性交",
                "Fisting": "拳交",
                "Fit two holes": "安装两个孔",
                "Footjob": "足交",
                "For Women": "对女性来说",
                "Gravure": "凹版印刷",
                "Hairy": "毛茸茸",
                "Handjob": "手淫",
                "HD DVD": "高清DVD",
                "Historical Play": "历史剧",
                "Hobby, Culture": "爱好，文化",
                "Horror": "恐怖",
                "How To": "如何",
                "Huge Butt": "巨臀",
                "Image Video": "图片视频",
                "Independents": "独立人士",
                "Interview": "面试",
                "Itching": "瘙痒",
                "Kiss": "吻",
                "Lolita": "洛丽塔",
                "Love": "爱",
                "Male Adventure": "男性冒险",
                "Massage": "按摩",
                "Masturbation": "自慰",
                "Mature Woman": "成熟女性",
                "Mini": "小型的",
                "MMF": "MMF",
                "MMFF": "MMFF",
                "Multiple Story": "多故事",
                "Muscle": "肌肉",
                "No bra": "没穿胸罩",
                "No underwear": "没有穿内衣",
                "Omnibus": "综合",
                "Original Collaboration": "原创合作",
                "Over 16 hours": "超过16小时",
                "Oversea Import": "海外进口",
                "Pampering": "宠爱",
                "Parody": "戏仿",
                "Piss Drinking": "喝尿",
                "POV": "第一人称视角",
                "Pregnant Woman": "孕妇",
                "Promiscuity": "滥交",
                "Psychological thriller": "心理惊悚片",
                "R-15": "R-15",
                "Reprinted Edition": "重印版",
                "Risky Mosaic": "冒险马赛克",
                "Roll your eyes": "翻白眼",
                "Sensuality": "感官",
                "SF": "旧金山",
                "Shave": "刮胡子",
                "Shaved": "剃光头",
                "Simulation": "模拟",
                "Slender": "苗条",
                "Solowork": "单人作品",
                "Spanking": "打屁股",
                "Special Effects": "特效",
                "Squirting": "喷水",
                "Subjectivity": "主观性",
                "Suspense": "悬念",
                "Tall": "高的",
                "Tits": "乳房",
                "Titty Fuck": "乳交",
                "Touch Typing": "盲打",
                "Transsexual": "变性人",
                "Ultra-Huge Tit": "超大乳房",
                "Uncensored Crack": "无删减版",
                "Uncensored Leak": "未删减版泄露",
                "Urination": "排尿",
                "US/EU Porn": "美国/欧盟色情",
                "User Submission": "用户提交",
                "Variety Show": "综艺节目",
                "VR": "VR",
                "Yoga·Fitness": "瑜伽·健身",
                "Cosplay": "角色扮演",
                "Couple": "夫妻",
                "College Students": "大学生",
                "Affair": "爱情故事",
                "Cuckold": "乌龟",
                "Bunny Girl": "兔女郎",
                "Cosplayers": "角色扮演者",
                "OL": "办公室女郎",
                "Model": "模型",
                "School Girls": "女学生",
                "Gal": "女孩",
                "Uniform": "制服",
                "Idol": "偶像",
                "Sweat": "汗",
                "Slut": "荡妇",
                "Nurse": "护士",
                "Older sister": "姐姐",
                "Married Woman": "已婚妇女",
                "Submissive Men": "顺从的男人",
                "Ultra-Huge Tits": "超大乳房",
                "Nasty, Hardcore": "恶心，硬核",
                "Virgin Man": "处女",
                "Abuse": "虐待",
                "Stepmother": "后妈",
                "Kimono, Mourning": "和服",
                "Female Teacher": "女教师",
                "Sister": "姐姐",
                "Pantyhose": "连裤袜",
                "Female Investigator": "女调查员",
                "Beauty Shop": "美容院",
                "Bus Guide": "巴士指南",
                "Miss": "错过",
                "Restraint": "克制",
                "Restraints": "约束",
                "Fetish": "恋物癖",
                "Humiliation": "屈辱"
            }

    def __init__(self):
        pass

    def __get_mediainfo(self, detail: dict, actors: list) -> MediaInfo:
        if not detail:
            return None

        mediainfo = MediaInfo()
        mediainfo.source = 'manko'
        mediainfo.type = MediaType.MOVIE

        # 获取媒体信息
        mediainfo.poster_path = detail.get("cover_image")
        mediainfo.backdrop_path = mediainfo.poster_path

        for title_name in detail.get("title_display_name", []):
            if title_name.get("language", "") == "en":
                mediainfo.en_title = title_name.get("title")
            else:
                mediainfo.title = title_name.get("title")
                mediainfo.original_title = mediainfo.title

            if title_name.get("language", "") == "ja":
                mediainfo.original_language = "Japanese"

        def sort_discription_of_languages(items: list) -> list:
            order = {"zh": 0, "en": 1, "ja": 2}
            return sorted(items, key=lambda x: (order.get(x["language"], 3), items.index(x)))

        sorted_desc = sort_discription_of_languages(detail.get("discription", []))
        overviews = [d.get("overview") for d in sorted_desc]
        mediainfo.overview = '\n'.join(overviews)
        mediainfo.release_date = detail.get("share_date")

        tags = detail.get("tags", [])
        if tags:
            tags_zh = [ self._tags_zh.get(tag, tag) for tag in tags ]
            mediainfo.tagline = ' '.join(tags_zh)

        mediainfo.vote_average = detail.get("imdb_score")

        if actors:
            _actors = []
            for actor in actors:
                _actors.append({
                    'adult': True,
                    'name': actor.get("name"),
                    'order': actor.get("order"),
                    'profile_path': actor.get("profile_path"),
                    'type': "Actor"})
            mediainfo.actors = _actors

        mediainfo.mediaid_prefix = 'manko'
        mediainfo.category = "AV"

        return mediainfo

    def search(self, title: str):
        mediainfos = []
        logger.info(f"Manko搜索：{title} ...")
        search_datas = self._get_api_json(self._search_url % title)
        if not search_datas:
            logger.warn("网站没有请求到数据")
            return None

        if search_datas.get("total", 0):
            logger.debug(f"{search_datas}")
            for search_data in search_datas.get("data", []):
                api_url = self._detail_url % search_data.get("_id")
                detail_data = self._get_api_json(api_url)
                actors_data = self._get_api_json(self._actors_url % search_data.get("_id"))
                if detail_data.get("data"):
                    detail = detail_data.get("data")
                    _mediainfo = self.__get_mediainfo(detail, actors_data.get("data"))
                    if _mediainfo:
                        _mediainfo.year = search_data.get("startyear")
                        logger.info(f"mediainfo={_mediainfo}")
                        mediainfos.append(_mediainfo)
                else:
                    logger.warn(f"请求api失败：{api_url}")
                    continue
        else:
            logger.warn("未搜索到结果，请尝试规范媒体名称（番号）")

        logger.info("解析完成")
        return mediainfos

    async def async_search(self, title: str):
        mediainfos = []
        logger.info(f"Manko搜索：{title} ...")
        search_datas = await self._async_get_api_json(self._search_url % title)
        if not search_datas:
            logger.warn("网站没有请求到数据")
            return None

        if search_datas.get("total", 0):
            logger.debug(f"{search_datas}")
            for search_data in search_datas.get("data", []):
                api_url = self._detail_url % search_data.get("_id")
                detail_data = await self._async_get_api_json(api_url)
                actors_data = await self._async_get_api_json(self._actors_url % search_data.get("_id"))
                if detail_data.get("data"):
                    detail = detail_data.get("data")
                    _mediainfo = self.__get_mediainfo(detail, actors_data.get("data"))
                    if _mediainfo:
                        _mediainfo.year = search_data.get("startyear")
                        logger.info(f"mediainfo={_mediainfo}")
                        mediainfos.append(_mediainfo)
                else:
                    logger.warn(f"请求api失败：{api_url}")
                    continue
        else:
            logger.warn("未搜索到结果，请尝试规范媒体名称（番号）")

        logger.info("解析完成")
        return mediainfos

    def test(self):
        ret = RequestUtils().get_res(self._base_url)
        if ret is None:
            return False
        return True

    def close(self):
        pass

    def _get_api_json(self, url: str) -> dict:
        raw_data = RequestUtils(accept_type="application/json").get_json(url=url)
        if raw_data:
            encrypt_data = raw_data.get("data")
            if encrypt_data:
                decrypt_data = self._base91_decode(encrypt_data)
                try:
                    raw_data["data"] = json.loads(decrypt_data)
                except Exception as e:
                    logger.error(f"数据base91解密失败，结果不是json数据！{e}")
                    raw_data["data"] = None
            return raw_data
        return {}

    async def _async_get_api_json(self, url: str) -> dict:
        raw_data = await AsyncRequestUtils(accept_type="application/json").get_json(url=url)
        if raw_data:
            encrypt_data = raw_data.get("data")
            if encrypt_data:
                decrypt_data = self._base91_decode(encrypt_data)
                try:
                    raw_data["data"] = json.loads(decrypt_data)
                except Exception as e:
                    logger.error(f"数据base91解密失败，结果不是json数据！{e}")
                    return {}
            return raw_data
        return {}

    @staticmethod
    def _base91_decode(encoded: str) -> str:
        alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789!#$%&()*+,./:;<=>?@[]^_`{|}~"'
        v = -1
        b = 0
        n = 0
        out = []
        for ch in encoded:
            try:
                c = alphabet.index(ch)
            except ValueError:
                continue
            if v == -1:
                v = c
            else:
                v += c * 91
                b |= v << n
                n += 13 if (v & 8191) > 88 else 14
                while n > 7:
                    out.append(b & 255)
                    b >>= 8
                    n -= 8
                v = -1
        if v != -1:
            out.append((b | (v << n)) & 255)
        return bytes(out).decode('utf-8', errors='replace')

class MankoModule(_ModuleBase):

    '''
    Manko媒体信息匹配
    '''
    # 元数据缓存
    cache: JAVCache = None
    # manko api
    manko: MankoApi = None
    # 刮削器
    scraper: JAVScraper = None

    def init_module(self) -> None:
        self.manko = MankoApi()
        self.scraper = JAVScraper()
        self.cache = JAVCache('manko')

    def stop(self):
        self.cache.save()
        self.manko.close()

    def test(self) -> Tuple[bool, str]:
        if self.manko.test():
            return False, "Manko网络连接失败"
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def get_name() -> str:
        return "Manko.fun"

    @staticmethod
    def get_type() -> ModuleType:
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        return MediaRecognizeType.TMDB

    @staticmethod
    def get_priority() -> int:
        return 0

    def recognize_media(self, meta: MetaBase = None,
                        mtype: Optional[MediaType] = None,
                        tmdbid: Optional[int] = None,
                        doubanid: Optional[str] = None,
                        bangumiid: Optional[int] = None,
                        episode_group: Optional[str] = None,
                        cache: bool = True) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param doubanid: 豆瓣ID
        :param bangumiid: BangumiID
        :param episode_group: 剧集组
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """

        mediainfos = []
        # 去掉年份
        search_name = re.sub(r' *\(\d+\)', '', meta.title)
        # 删除特殊字符
        search_name = re.sub(r'[() ，,：:&$]', '', search_name)

        if cache:
            # 读取缓存
            cache_info = self.cache.get(meta)
            if cache_info:
                cache_data = cache_info.get('data')
                if not cache_data:
                    if cache_info.get("error", 0) >= 3:
                        return None
                elif cache_data.title == search_name:
                    mediainfos = [cache_data]

        try:
            if not mediainfos:
                mediainfos = self.manko.search(search_name)

            logger.debug(f"mediainfos={mediainfos}")

            if not mediainfos:
                self.cache.update(meta, None)
                return None

            _mediainfo: MediaInfo = mediainfos[0]

            self.cache.update(meta, _mediainfo)
        except Exception as e:
            self.cache.update(meta, None)
            raise e

        return _mediainfo

    async def async_recognize_media(self, meta: MetaBase = None,
                        mtype: Optional[MediaType] = None,
                        tmdbid: Optional[int] = None,
                        doubanid: Optional[str] = None,
                        bangumiid: Optional[int] = None,
                        episode_group: Optional[str] = None,
                        cache: bool = True) -> Optional[MediaInfo]:
        """
        识别媒体信息，不含Fanart图片
        :param meta:     识别的元数据
        :param mtype:    识别的媒体类型，与tmdbid配套
        :param tmdbid:   tmdbid
        :param doubanid: 豆瓣ID
        :param bangumiid: BangumiID
        :param episode_group: 剧集组
        :param cache:    是否使用缓存
        :return: 识别的媒体信息，包括剧集信息
        """

        mediainfos = []
        # 去掉年份
        search_name = re.sub(r' *\(\d+\)', '', meta.title)
        # 删除特殊字符
        search_name = re.sub(r'[() ，,：:&$]', '', search_name)

        if cache:
            # 读取缓存
            cache_info = self.cache.get(meta)
            if cache_info:
                cache_data = cache_info.get('data')
                if not cache_data:
                    if cache_info.get("error", 0) >= 3:
                        return None
                elif cache_data.title == search_name:
                    mediainfos = [cache_data]

        try:
            if not mediainfos:
                mediainfos = await self.manko.async_search(search_name)

            logger.debug(f"mediainfos={mediainfos}")

            if not mediainfos:
                self.cache.update(meta, None)
                return None

            _mediainfo: MediaInfo = mediainfos[0]

            self.cache.update(meta, _mediainfo)
        except Exception as e:
            self.cache.update(meta, None)
            raise e

        return _mediainfo

    def scheduler_job(self) -> None:
        """
        定时任务，每10分钟调用一次
        """
        self.cache.save()

    def clear_cache(self):
        """
        清除缓存
        """
        logger.info("开始清除Manko缓存 ...")
        self.cache.clear()
        logger.info("Manko缓存清除完成")
