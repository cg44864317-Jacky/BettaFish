# -*- coding: utf-8 -*-
"""
每日跟踪：对处于跟踪期的视频批量拉取最新点赞、评论、转发、收藏，写入每日快照。
- 跟踪期满（默认 14 天）自动停止
- 连续多次查不到（删除 / 私密 / 仅部分可见）标记为 unavailable 并停止
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from loguru import logger
from sqlalchemy import select

from .db import session_scope
from .discovery import upsert_snapshot
from .models import DouyinStockVideo
from .settings import MonitorSettings, get_settings
from .sources import SourceError, TrackingSource


@dataclass
class TrackStats:
    active: int = 0
    updated: int = 0
    expired: int = 0
    missing: int = 0
    unavailable: int = 0
    api_calls: int = 0
    unavailable_ids: List[str] = field(default_factory=list)


class DailyTracker:
    def __init__(self, tracking: TrackingSource, settings: Optional[MonitorSettings] = None):
        self.tracking = tracking
        self.s = settings or get_settings()

    def run(self, now: Optional[datetime] = None) -> TrackStats:
        now = now or datetime.now()
        stats = TrackStats()
        with session_scope() as s:
            rows = list(
                s.scalars(
                    select(DouyinStockVideo).where(
                        DouyinStockVideo.is_tracked.is_(True), DouyinStockVideo.tracking_status == "active"
                    )
                )
            )
            for r in rows:
                if r.track_until and r.track_until < now:
                    r.tracking_status = "expired"
                    stats.expired += 1
        # 今天已经在发现阶段拍过快照的新视频不必重复查询
        todo = [
            r.aweme_id for r in rows
            if not (r.track_until and r.track_until < now)
            and not (r.last_tracked_at and r.last_tracked_at.date() == now.date())
        ]
        stats.active = len(todo)
        if not todo:
            logger.info(f"[跟踪] 没有需要更新的视频（到期 {stats.expired} 条）")
            return stats

        calls_before = getattr(getattr(self.tracking, "client", None), "calls", 0)
        try:
            found = self.tracking.fetch_videos(todo)
        except SourceError as e:
            logger.error(f"[跟踪] 拉取失败，今天的快照缺失：{e}")
            return stats
        stats.api_calls = getattr(getattr(self.tracking, "client", None), "calls", 0) - calls_before

        with session_scope() as s:
            for aid in todo:
                row = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == aid))
                rec = found.get(aid)
                if rec is None:
                    row.miss_count = (row.miss_count or 0) + 1
                    stats.missing += 1
                    if row.miss_count >= self.s.STOCK_VIDEO_UNAVAILABLE_AFTER_MISSES:
                        row.tracking_status = "unavailable"
                        stats.unavailable += 1
                        stats.unavailable_ids.append(aid)
                    continue
                row.miss_count = 0
                for attr in ("liked_count", "comment_count", "share_count", "collected_count"):
                    if getattr(rec, attr) is not None:
                        setattr(row, attr, getattr(rec, attr))
                row.last_tracked_at = now
                upsert_snapshot(s, aid, rec, now.date(), now)
                stats.updated += 1
        logger.info(
            f"[跟踪] 更新 {stats.updated}/{stats.active} 条，到期 {stats.expired}，查不到 {stats.missing}"
            f"（判定不可见 {stats.unavailable}），TikHub 调用 {stats.api_calls} 次"
        )
        return stats
