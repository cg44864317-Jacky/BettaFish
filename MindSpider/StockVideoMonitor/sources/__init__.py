# -*- coding: utf-8 -*-
"""数据源工厂：关键词搜索支持主备切换，跟踪和博主池固定走 TikHub。"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from loguru import logger

from ..settings import MonitorSettings, get_settings
from .base import DiscoverySource, SourceError, TrackingSource, VideoRecord

__all__ = [
    "DiscoverySource", "TrackingSource", "VideoRecord", "SourceError",
    "FallbackDiscovery", "build_discovery", "build_tracking",
]


class FallbackDiscovery(DiscoverySource):
    """按顺序尝试多个数据源，前一个失败就用下一个。"""

    name = "fallback"

    def __init__(self, sources: List[DiscoverySource]):
        if not sources:
            raise SourceError("没有可用的发现数据源，请检查 TikHub / MediaCrawler 配置")
        self.sources = sources
        self.used: Optional[str] = None

    def search_recent(self, keywords: List[str]) -> Dict[str, List[VideoRecord]]:
        errors = []
        for src in self.sources:
            try:
                result = src.search_recent(keywords)
                self.used = src.name
                return result
            except SourceError as e:
                logger.warning(f"[发现] {src.name} 搜索失败，尝试下一个数据源：{e}")
                errors.append(f"{src.name}: {e}")
        raise SourceError("所有数据源搜索均失败 —— " + "；".join(errors))

    def creator_recent_posts(self, sec_uids: Iterable[str], since_ts: int) -> Dict[str, List[VideoRecord]]:
        sec_uids = list(sec_uids)
        for src in self.sources:
            try:
                return src.creator_recent_posts(sec_uids, since_ts)
            except SourceError:
                continue
        return {}


def _try_tikhub(settings: MonitorSettings):
    try:
        from .tikhub import TikHubSource

        return TikHubSource(settings=settings)
    except SourceError as e:
        logger.warning(f"TikHub 不可用：{e}")
        return None


def _try_mediacrawler(settings: MonitorSettings):
    try:
        from .mediacrawler import MediaCrawlerSource

        return MediaCrawlerSource(settings=settings)
    except SourceError as e:
        logger.warning(f"MediaCrawler 不可用：{e}")
        return None


def build_discovery(settings: Optional[MonitorSettings] = None) -> FallbackDiscovery:
    s = settings or get_settings()
    tikhub, mc = _try_tikhub(s), None
    primary = (s.STOCK_VIDEO_DISCOVERY_SOURCE or "mediacrawler").lower()
    if primary == "mediacrawler":
        mc = _try_mediacrawler(s)
        order = [mc, tikhub]
    else:
        order = [tikhub, _try_mediacrawler(s)]
    return FallbackDiscovery([x for x in order if x is not None])


def build_tracking(settings: Optional[MonitorSettings] = None) -> TrackingSource:
    s = settings or get_settings()
    src = _try_tikhub(s)
    if src is None:
        raise SourceError("每日跟踪需要 TikHub，请配置 TIKHUB_API_KEY")
    return src
