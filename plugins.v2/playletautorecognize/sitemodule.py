import re
import requests
import datetime
import subprocess

from typing import Optional, Tuple, Union
from pathlib import Path
from lxml import etree
from bencode import bdecode

from app.chain.search import SearchChain
from app.core.meta import MetaBase
from app.core.context import MediaInfo, Context, TorrentInfo, MetaInfo
from app.modules import _ModuleBase
from app.log import logger
from app.schemas.types import MediaType, ModuleType, MediaRecognizeType
from app.db.site_oper import SiteOper
from app.utils.string import StringUtils

from .myutils import get_page_source
from .myutils import PlayletCache, PlayletScraper

class SiteApi():
    _searchsites = []
    _torrent_dirs = []
    _last_page_cache = []
    _last_torrent_info = []

    def __init__(self, searchsites, torrent_dirs):
        self._searchsites = searchsites
        self._torrent_dirs = torrent_dirs
        self._session = requests.Session()

    def close(self):
        if self._session:
            self._session.close()

    def __site_meta_update(self, meta, info, cover: bool = False):
        '''
        使用元数据补全搜索到的媒体信息
        :param meta: 文件元数据
        :param info:站点搜索到的媒体元数据
        :param cover: 是否覆盖站点搜索的媒体元数据
        :return: 更新后的媒体元数据
        '''
        if not info.get('org_string') or cover:
            info['org_string'] = meta.org_string
        if not info.get('cn_name') or cover:
            info['cn_name'] = meta.cn_name
        if not info.get('en_name') or cover:
            info['en_name'] = meta.en_name
        if not info.get('subtitle') or cover:
            info['subtitle'] = meta.subtitle
        if not info.get('begin_season') or cover:
            info['begin_season'] = meta.begin_season
        if not info.get('total_season') or cover:
            info['total_season'] = meta.total_season
        if not info.get('total_episode') or cover:
            info['total_episode'] = meta.total_episode
        if not info.get('season_episode') or cover:
            info['season_episode'] = "S01"
        return info

    def __site_get_context(self, meta: MetaBase) -> dict:
        '''
        从站点搜索种子
        :param meta: 文件元数据
        :return: 最符合的种子信息
        '''
        def _site_comparison_meta_name(oldmeta, newmeta):
            '''
            粗略的识别是不是搜索到了种子
            :param name: 媒体的中文标题
            :param tv_name: 搜索到的种子标题
            :return: 是否是这个种子
            '''

            meta_org_name = newmeta.get('org_string')
            if not meta_org_name:
                return False

            if oldmeta.cn_name:
                tv_name = meta_org_name.replace('（', '(').replace('）', ')').replace('＆', '&')
                tv_name = tv_name.replace('$', ' ').replace('&', ' ').strip()
                match = re.match(r'^(.*?)\(([全共]?\d+)[集话話期幕][全完]?\)(?:&?([^&]+))?', tv_name)
                if match:
                    title = match.group(1).strip().split('(')[0]
                else:
                    title = tv_name.split('(')[0]

                if oldmeta.cn_name == title:
                    return True
                elif len(oldmeta.cn_name) > 6 and oldmeta.cn_name in title:
                        return True

            if oldmeta.en_name:
                # 如果是英文名称，直接比较
                if oldmeta.en_name in meta_org_name:
                    return True
                else:
                    # 逐个比较英文名称的单词, 允许两个多音单词
                    fail_cnt = 0
                    newwords = meta_org_name.split()
                    for index, word in enumerate(oldmeta.en_name.split()):
                        start_split = index-1 if index-1 >= 0 else 0
                        end_split = index+2 if index+2 < len(newwords) else -1
                        if not word in newwords[start_split:end_split]:
                            if word.isdigit(): #数字必须在标题中
                                return False
                            fail_cnt += 1
                            if fail_cnt >= 2:
                                return False
                    return True

            return False

        site_contexts = []
        torrents = SearchChain().last_search_results()
        if torrents:
            for torrent in torrents:
                _context = torrent.to_dict()
                logger.debug(f"context1: {_context.get('meta_info').get('org_string')}")
                meta_cn_name = _context.get('meta_info').get('cn_name')
                meta_cn_name = meta_cn_name.replace('$', ' ').replace('&', ' ').strip() if meta_cn_name else None
                meta_en_name = _context.get('meta_info').get('en_name')
                meta_en_name = meta_en_name.replace('$', ' ').replace('&', ' ').strip() if meta_en_name else None
                if (meta.en_name and meta.en_name == meta_en_name) or (meta.cn_name and meta.cn_name == meta_cn_name):
                    _context['meta_info'] = self.__site_meta_update(meta, _context.get('meta_info'))
                    site_contexts.append(_context)
                elif _site_comparison_meta_name(meta, _context.get('meta_info')):
                    _context['meta_info'] = self.__site_meta_update(meta, _context.get('meta_info'), True)
                    site_contexts.append(_context)

        if len(site_contexts) == 0:
            torrents = SearchChain().search_by_title(title=meta.cn_name, sites=self._searchsites, cache_local=True)
            if torrents:
                for torrent in torrents:
                    _context = torrent.to_dict()
                    logger.debug(f"context2: {_context.get('meta_info').get('org_string')}")
                    meta_cn_name = _context.get('meta_info').get('cn_name')
                    meta_cn_name = meta_cn_name.replace('$', ' ').replace('&', ' ').strip() if meta_cn_name else None
                    meta_en_name = _context.get('meta_info').get('en_name')
                    meta_en_name = meta_en_name.replace('$', ' ').replace('&', ' ').strip() if meta_en_name else None
                    if (meta.en_name and meta.en_name == meta_en_name) or (meta.cn_name and meta.cn_name == meta_cn_name):
                        _context['meta_info'] = self.__site_meta_update(meta, _context.get('meta_info'))
                        site_contexts.append(_context)
                    elif _site_comparison_meta_name(meta, _context.get('meta_info')):
                        _context['meta_info'] = self.__site_meta_update(meta, _context.get('meta_info'), True)
                        site_contexts.append(_context)
            else:
                return {}

        logger.debug(f"site_contexts={site_contexts}")

        site_contexts_year = []
        if len(site_contexts) == 0:
            return {}

        if len(site_contexts) == 1:
            return site_contexts[0]
        else:
            _year_priority = []
            _year_null = []
            # 有year的放前面
            for _m in site_contexts:
                if _m.get('meta_info').get('year'):
                    _year_priority.append(_m)
                else:
                    _year_null.append(_m)
            if _year_priority:
                _year_priority.extend(_year_null)
            else:
                _year_priority = _year_null
            site_contexts = _year_priority
            if meta.year:
                for _m in site_contexts:
                    if meta.year == _m.get('meta_info').get('year'):
                        site_contexts_year.append(_m)

        site_contexts_edition = []
        if len(site_contexts_year) == 0:
            return site_contexts[0]

        if len(site_contexts_year) == 1:
            return site_contexts_year[0]
        else:
            _edition_priority = []
            _edition_null = []
            # 有edition的放前面
            for _m in site_contexts_year:
                if _m.get('meta_info').get('edition'):
                    _edition_priority.append(_m)
                else:
                    _edition_null.append(_m)
            if _edition_priority:
                _edition_priority.extend(_edition_null)
            else:
                _edition_priority = _edition_null
            site_contexts_year = _edition_priority
            if meta.edition:
                for _m in site_contexts_year:
                    if not meta.edition and meta.edition == _m.get('meta_info').get('edition'):
                        site_contexts_edition.append(_m)

        site_contexts_season = []
        if len(site_contexts_edition) == 0:
            return site_contexts_year[0]

        if len(site_contexts_edition) == 1:
            return site_contexts_edition[0]
        else:
            for _m in site_contexts_edition:
                if meta.is_in_season(_m.get('meta_info').get('season_episode')):
                    site_contexts_season.append(_m)

        if len(site_contexts_edition) == 0:
            return site_contexts_edition[0]
        else:
            return site_contexts_season[0]

    def __site_brief_text(self, torrent: dict):
        '''
        获取种子详情页
        :param torrent: 种子信息
        :return: 页面text
        '''
        url = torrent.get("page_url")
        if self._last_page_cache and self._last_page_cache[0] == url:
            return self._last_page_cache[1]

        site = SiteOper().get(torrent.get('site'))
        # 获取种子详情页
        torrent_detail_source = get_page_source(url=url, session=self._session, cookies=site.cookie, timeout=30)
        if not torrent_detail_source:
            logger.error(f"请求种子详情页失败 {url}")
            return None

        html = etree.HTML(torrent_detail_source)
        if not html:
            logger.error(f"详情页无数据 {url}")
            return None

        self._last_page_cache = [url, html]

        return html

    def __get_last_form_torrent(self, meta):
        if self._last_torrent_info and self._last_torrent_info[0] == meta.org_string:
            return self._last_torrent_info[1]
        return None

    def __get_info_form_torrent(self, meta):
        if not self._torrent_dirs:
            return None

        grep_cmd = ['grep']
        if meta.org_string:
            grep_cmd.extend(['-r', rf'"{meta.org_string}"'])
        else:
            return None

        torrent_result = []
        # 使用grep查找包含标题的种子文件
        for torrent_dir in self._torrent_dirs:
            grep_cmd.append(torrent_dir)
            ret = subprocess.getstatusoutput(' '.join(grep_cmd))
            if ret[0] < 0:
                logger.error(f"查找目录下 {torrent_dir} 种子文件错误：{ret[1]}")
                continue
            if ret[0] == 0:
                for msg in ret[1].split('\n'):
                    match = re.match(r'grep: (.*): binary file matches$', msg)
                    if match:
                        torrent_result.append(match.group(1))

        if not torrent_result:
            return None

        torrent_1dicts = []
        for torrent in torrent_result:
            torrent_path = Path(torrent)
            torrent_data = torrent_path.read_bytes()
            torrent_detail = bdecode(torrent_data)
            comment = torrent_detail.get('comment')
            if not comment:
                continue
            url_index = comment.find('http')
            if url_index >= 0:
                url = comment[url_index:]
            else:
                continue
            domain = StringUtils.get_url_domain(url)
            # 检查url是否在mp站点中
            site = SiteOper().get_by_domain(domain)
            if not site:
                logger.info(f"没有站点：{domain}")
                continue
            # 检查站点有没有被选择来识别短剧
            if not site.id in self._searchsites:
                logger.info(f"可以添加站点到识别列表中：{site.name}")
                continue

            torrent_1dicts.append([site, url])

        # 按选择的站点顺序排序
        torrent_2dicts = []
        for id in self._searchsites:
            for _info in torrent_1dicts:
                if id == _info[0].id:
                    torrent_2dicts.append(_info)

        # 按顺序，找到数据就返回
        for _info in torrent_2dicts:
            site = _info[0]
            url = _info[1]
            torrent_info = TorrentInfo(site=site.id,
                                    site_name=site.name,
                                    site_cookie=site.cookie,
                                    site_ua=site.ua,
                                    site_proxy=site.proxy,
                                    site_order=site.pri,
                                    site_downloader=site.downloader,
                                    page_url=url)
            html = self.__site_brief_text(torrent_info.to_dict())
            if not html:
                continue
            title_text = html.xpath("//h1[@id='top']/text()")
            title = ' '.join(title_text).strip()
            subtitle = ""
            tr_elements = html.xpath('//tr')
            for tr in tr_elements:
                key_column_value = tr.xpath('.//td[1]/text()')
                if key_column_value:
                    key_column_value = key_column_value[0].strip()
                    if key_column_value == '副标题':
                        value_column_value = tr.xpath('.//td[2]/text()')
                        if value_column_value:
                            subtitle = value_column_value[0].strip()
                    elif key_column_value == '下载':
                        value_column_value = tr.xpath('.//td[2]/span/@title')
                        if value_column_value:
                            torrent_info.pubdate = value_column_value[0]
                    elif key_column_value == '标签':
                        value_column_value = tr.xpath('.//td[2]/span/text()')
                        if value_column_value:
                            torrent_info.labels = value_column_value

            meta_info = MetaInfo(title=title, subtitle=subtitle)
            context = Context(meta_info=meta_info, torrent_info=torrent_info)

            logger.debug(f"context={context}")
            self._last_torrent_info = [meta.org_string, context.to_dict()]
            return self._last_torrent_info[1]

        return None

    def search(self, meta: MetaBase):
        '''
        从站点识别媒体信息
        :param meta: 文件元数据
        :return: 媒体元数据
        '''
        # 先检查种子搜索有没有
        context = self.__get_last_form_torrent(meta)
        if not context:
            # 搜索站点
            context = self.__site_get_context(meta)
        if not context:
            # 搜索种子文件
            context = self.__get_info_form_torrent(meta)

        if not context:
            return None

        html = self.__site_brief_text(context.get('torrent_info'))
        if not html:
            return None
        brief_texts = html.xpath("//td[contains(@class, 'rowfollow')]/div[@id='kdescr']")
        if not brief_texts:
            return None

        logger.debug(f"从种子详情页提取数据 ...")

        img_url = None
        images = brief_texts[0].xpath(".//img[1]/@src")
        if not images:
            logger.error(f"未获取到种子封面图 {context.get('torrent_info').get("page_url")}")
        else:
            img_url = str(images[0])
        logger.info(f"获取到种子封面图 {img_url}")

        brief_text = brief_texts[0].xpath("string()").strip()
        brief = None
        if brief_text and len(brief_text) > 5:
            brief_match = re.search(r'简\s*介\s*[：: ]?([^◎]*)', brief_text)
            if brief_match:
                brief = brief_match.group(1).strip()
            else:
                brief = brief_text.strip()

        subtitle = context.get('meta_info').get('subtitle')
        tags = []
        if subtitle and '类型' in subtitle:
            subtitle_strs = subtitle.split('|')
            for s in subtitle_strs:
                if '类型' in s:
                    tags = s.replace('类型', '').replace(':', '').replace('：', '').replace('/', ' ').split()
        if 'labels' in context.get('torrent_info').keys():
            for _l in context.get('torrent_info').get('labels'):
                if '禁转' in _l or '官方' in _l or '短剧' in _l or re.search(r'\d+[月天时分]', _l):
                    pass
                else:
                    tags.append(_l)

        actors = []
        if subtitle and '演员' in subtitle:
            subtitle_strs = subtitle.split('|')
            for s in subtitle_strs:
                if '演员' in s:
                    actors = [{ 'name': elem, 'type': 'Actor' } for elem in s.replace('演员', '').replace(':', '').replace('：', '').split()]

        logger.info(f"获取tag: {tags}")
        logger.debug(f"简介: {brief}")

        mediainfo = MediaInfo()
        mediainfo.source = 'site'
        mediainfo.type = MediaType.TV
        mediainfo.title = context.get('meta_info').get('cn_name')
        mediainfo.en_title = context.get('meta_info').get('en_name')
        mediainfo.year = context.get('meta_info').get('year')
        if not mediainfo.year:
            datetime_object = datetime.datetime.strptime(context.get('torrent_info').get('pubdate'), '%Y-%m-%d %H:%M:%S')
            mediainfo.year = datetime_object.year

        season_episode = context.get('meta_info').get('season_episode')
        if season_episode:
            season_match = re.search(r'S(\d+)', season_episode)
            if season_match:
                mediainfo.season = int(season_match.group(1))
            elif mediainfo.season is None:
                mediainfo.season = 1
        mediainfo.original_title = context.get('meta_info').get('cn_name')
        mediainfo.backdrop_path = img_url
        mediainfo.poster_path = img_url
        mediainfo.category = "短剧"
        mediainfo.number_of_episodes = context.get('meta_info').get('total_episode')
        mediainfo.number_of_seasons = context.get('meta_info').get('total_season')
        mediainfo.overview = brief
        mediainfo.mediaid_prefix = context.get('torrent_info').get('site_name')
        mediainfo.media_id = context.get('torrent_info').get('site')
        mediainfo.release_date = context.get('torrent_info').get('pubdate')
        mediainfo.tagline = ' '.join(tags) if tags else None
        mediainfo.actors = actors

        return [mediainfo]

class SiteModule(_ModuleBase):
    '''
    站点媒体信息匹配
    '''
    # 元数据缓存
    cache: PlayletCache = None
    # 站点
    site: SiteApi = None
    # 刮削器
    scraper: PlayletScraper = None

    _searchsites = []
    _torrent_dirs = []

    def __init__(self, searchsites, torrent_dirs: list = []) -> None:
        super().__init__()
        self._searchsites = searchsites
        self._torrent_dirs = torrent_dirs

    def init_module(self) -> None:
        self.site = SiteApi(self._searchsites, self._torrent_dirs)
        self.scraper = PlayletScraper()
        self.cache = PlayletCache('site')

    def stop(self):
        self.cache.save()
        self.site.close()

    def test(self) -> Tuple[bool, str]:
        return True, ""

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    @staticmethod
    def get_name() -> str:
        return "站点"

    @staticmethod
    def get_type() -> ModuleType:
        return ModuleType.MediaRecognize

    @staticmethod
    def get_subtype() -> MediaRecognizeType:
        return MediaRecognizeType.TMDB

    @staticmethod
    def get_priority() -> int:
        return 1

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
        if cache:
            # 读取缓存
            cache_info = self.cache.get(meta)
            if cache_info:
                cache_data = cache_info.get('data')
                if not cache_data:
                    if cache_info.get("error") >= 3:
                        return None
                elif cache_data.title == meta.cn_name:
                    mediainfos = [cache_data]

        try:
            if not mediainfos:
                mediainfos = self.site.search(meta)

            logger.debug(f"mediainfos={mediainfos}")

            if not mediainfos:
                self.cache.update(meta, None)
                return None

            _mediainfo: MediaInfo = mediainfos[0]
            if len(mediainfos) > 1:
                for _m in mediainfos:
                    if _m.title == meta.cn_name:
                        _mediainfo = _m
                        break

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
        logger.info("开始清除站点缓存 ...")
        self.cache.clear()
        logger.info("站点缓存清除完成")
