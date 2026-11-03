# -*- coding: utf-8 -*-
"""
每日发现与识别

三路召回 → 去重入库 → 粗筛 → 刷新详情（拿新鲜的下载地址）→ 语音/画面解析 → 精判打标
视频和图文作品都处理：视频做语音识别 + 抽帧文字识别，图文直接对每张图片做文字识别。
被判定为「上市公司分析」的视频进入 14 天跟踪，作者自动加入财经博主池。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import select

from .classifier import TwoStageClassifier
from .db import session_scope
from .knowledge_base import fetch_hot_stock_names
from .media import MediaAnalyzer
from .models import (
    DouyinFinanceCreator, DouyinStockVideo, DouyinStockVideoCompany, DouyinStockVideoSnapshot, dumps, loads_list,
)
from .settings import MonitorSettings, get_settings
from .sources import DiscoverySource, SourceError, TrackingSource, VideoRecord


@dataclass
class DiscoveryStats:
    candidates: int = 0
    new_videos: int = 0
    finance: int = 0
    flagged: int = 0
    failed: int = 0
    source_used: Optional[str] = None
    channel_counts: Dict[str, int] = field(default_factory=dict)


def merge_records(groups: Dict[str, List[VideoRecord]]) -> Dict[str, VideoRecord]:
    """多路召回结果去重，记录每条视频是被哪些路径找到的。"""
    merged: Dict[str, VideoRecord] = {}
    for channel, records in groups.items():
        for r in records:
            existing = merged.get(r.aweme_id)
            if existing is None:
                r.channels = [channel]
                merged[r.aweme_id] = r
            elif channel not in existing.channels:
                existing.channels.append(channel)
    return merged


def upsert_snapshot(session, aweme_id: str, rec: VideoRecord, snap_date: date, now: datetime) -> None:
    snap = session.scalar(
        select(DouyinStockVideoSnapshot).where(
            DouyinStockVideoSnapshot.aweme_id == aweme_id, DouyinStockVideoSnapshot.snapshot_date == snap_date
        )
    )
    if snap is None:
        snap = DouyinStockVideoSnapshot(aweme_id=aweme_id, snapshot_date=snap_date)
        session.add(snap)
    snap.captured_at = now
    snap.liked_count, snap.comment_count = rec.liked_count, rec.comment_count
    snap.share_count, snap.collected_count = rec.share_count, rec.collected_count


class DailyDiscovery:
    def __init__(
        self,
        discovery: DiscoverySource,
        classifier: TwoStageClassifier,
        analyzer: MediaAnalyzer,
        tracking: Optional[TrackingSource] = None,
        settings: Optional[MonitorSettings] = None,
        hot_keywords_fn=fetch_hot_stock_names,
    ):
        self.discovery = discovery
        self.classifier = classifier
        self.analyzer = analyzer
        self.tracking = tracking
        self.s = settings or get_settings()
        self.hot_keywords_fn = hot_keywords_fn

    # -------------------------------------------------------------- 召回
    def collect(self, now: datetime, stats: DiscoveryStats) -> Dict[str, VideoRecord]:
        since_ts = int((now - timedelta(hours=self.s.STOCK_VIDEO_LOOKBACK_HOURS)).timestamp())
        groups: Dict[str, List[VideoRecord]] = {}

        base_kw = list(dict.fromkeys(self.s.STOCK_VIDEO_SEARCH_KEYWORDS))
        hot_kw = [k for k in self.hot_keywords_fn(self.s.STOCK_VIDEO_HOT_STOCK_KEYWORDS) if k not in base_kw]
        try:
            by_kw = self.discovery.search_recent(base_kw + hot_kw)
            stats.source_used = getattr(self.discovery, "used", None) or self.discovery.name
            for kw, recs in by_kw.items():
                groups[f"{'hot' if kw in hot_kw else 'keyword'}:{kw}"] = recs
        except SourceError as e:
            logger.error(f"[发现] 关键词搜索全部失败：{e}")

        if self.s.STOCK_VIDEO_CREATOR_POOL_ENABLED:
            with session_scope() as s:
                creators = list(
                    s.scalars(
                        select(DouyinFinanceCreator.sec_uid)
                        .where(DouyinFinanceCreator.enabled.is_(True))
                        .order_by(DouyinFinanceCreator.last_flagged_at.desc())
                        .limit(self.s.STOCK_VIDEO_CREATOR_POOL_MAX)
                    )
                )
            if creators:
                posts = self.discovery.creator_recent_posts(creators, since_ts)
                groups["creator"] = [r for recs in posts.values() for r in recs]
                with session_scope() as s:
                    for uid in posts:
                        c = s.get(DouyinFinanceCreator, uid)
                        if c:
                            c.last_fetched_at = now

        stats.channel_counts = {k: len(v) for k, v in groups.items()}
        merged = merge_records(groups)
        fresh = {k: r for k, r in merged.items() if r.create_time is None or r.create_time >= since_ts}
        stats.candidates = len(fresh)
        return fresh

    # -------------------------------------------------------------- 入库
    def store_new(self, records: Dict[str, VideoRecord], now: datetime, stats: DiscoveryStats) -> List[str]:
        if not records:
            return []
        with session_scope() as s:
            existing = set(
                s.scalars(select(DouyinStockVideo.aweme_id).where(DouyinStockVideo.aweme_id.in_(list(records))))
            )
            new_ids = []
            for aid, r in records.items():
                if aid in existing:
                    continue
                s.add(
                    DouyinStockVideo(
                        aweme_id=aid, title=r.title, desc=r.desc, hashtags=dumps(r.hashtags),
                        create_time=r.create_time, author_uid=r.author_uid, sec_uid=r.sec_uid, nickname=r.nickname,
                        follower_count=r.follower_count, aweme_url=r.aweme_url, video_url=r.video_url,
                        cover_url=r.cover_url, duration_ms=r.duration_ms, aweme_type=r.aweme_type,
                        is_image_post=r.is_image_post, image_urls=dumps(r.image_urls), discovered_at=now,
                        discovery_source=r.source, discovery_channels=dumps(r.channels), stage="new",
                        liked_count=r.liked_count, comment_count=r.comment_count, share_count=r.share_count,
                        collected_count=r.collected_count, is_tracked=False, tracking_status="none", miss_count=0,
                    )
                )
                new_ids.append(aid)
        stats.new_videos = len(new_ids)
        return new_ids

    # -------------------------------------------------------------- 识别
    def classify(self, aweme_ids: List[str], now: datetime, stats: DiscoveryStats) -> None:
        if not aweme_ids:
            return
        with session_scope() as s:
            rows = list(s.scalars(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id.in_(aweme_ids))))
        texts = {r.aweme_id: "\n".join(x for x in (r.title, r.desc, " ".join("#" + t for t in loads_list(r.hashtags))) if x) for r in rows}
        verdicts = self.classifier.prefilter([texts[r.aweme_id] for r in rows])
        finance_ids = [r.aweme_id for r, ok in zip(rows, verdicts) if ok]
        with session_scope() as s:
            for r, ok in zip(rows, verdicts):
                row = s.get(DouyinStockVideo, r.id)
                row.finance_related = ok
                row.stage = "finance" if ok else "prefilter_rejected"
        stats.finance = len(finance_ids)
        logger.info(f"[识别] 粗筛：{len(rows)} 条中 {len(finance_ids)} 条与股票相关")

        fresh: Dict[str, VideoRecord] = {}
        if self.tracking and finance_ids:  # 播放地址几小时就会过期，解析前刷新一次，顺便拿到最新互动数
            try:
                fresh = self.tracking.fetch_videos(finance_ids)
            except SourceError as e:
                logger.warning(f"[识别] 刷新视频详情失败，使用发现时的地址：{e}")

        for i, aid in enumerate(finance_ids, 1):
            try:
                self._judge_one(aid, texts[aid], fresh.get(aid), now, stats)
            except Exception as e:
                stats.failed += 1
                logger.exception(f"[识别] {aid} 处理失败：{e}")
                with session_scope() as s:
                    row = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == aid))
                    row.stage, row.error_message = "failed", str(e)[:1000]
            if i % 20 == 0:
                logger.info(f"[识别] 精判进度 {i}/{len(finance_ids)}，已标记 {stats.flagged} 条")

    def _judge_one(self, aid: str, text: str, fresh: Optional[VideoRecord], now: datetime, stats: DiscoveryStats) -> None:
        with session_scope() as s:
            row = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == aid))
            video_url = (fresh.video_url if fresh else None) or row.video_url
            image_urls = (fresh.image_urls if fresh else None) or loads_list(row.image_urls)
            is_image = row.is_image_post or bool(fresh and fresh.is_image_post)
        asr_text, ocr_text = "", ""
        try:
            if is_image:
                asr_text, ocr_text = "", self.analyzer.analyze_images(aid, image_urls)
            else:
                asr_text, ocr_text = self.analyzer.analyze(aid, video_url)
        except Exception as e:  # 解析失败不影响用文字判断
            logger.warning(f"[识别] {aid} 语音/画面解析失败，仅用文字判断：{e}")
        result = self.classifier.judge(text, asr_text, ocr_text)

        with session_scope() as s:
            row = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == aid))
            if fresh:
                row.video_url = fresh.video_url or row.video_url
                if fresh.image_urls:
                    row.image_urls = dumps(fresh.image_urls)
                for attr in ("liked_count", "comment_count", "share_count", "collected_count"):
                    if getattr(fresh, attr) is not None:
                        setattr(row, attr, getattr(fresh, attr))
            row.asr_text, row.ocr_text = asr_text or None, ocr_text or None
            row.is_company_analysis = result.is_company_analysis
            row.analysis_confidence = result.confidence
            row.analysis_summary = result.summary
            row.analysis_raw = dumps(result.raw) if result.raw is not None else None
            row.stage = "analyzed"
            if not result.is_company_analysis:
                return
            stats.flagged += 1
            row.is_tracked, row.tracking_status = True, "active"
            row.track_until = now + timedelta(days=self.s.STOCK_VIDEO_TRACK_DAYS)
            for m in result.companies:
                s.add(
                    DouyinStockVideoCompany(
                        aweme_id=aid, stock_code=m.stock_code, stock_name=m.stock_name, basis=m.basis,
                        stance=m.stance, evidence=m.evidence, confidence=m.confidence, video_create_time=row.create_time,
                    )
                )
            snap_src = fresh or VideoRecord(
                aweme_id=aid, liked_count=row.liked_count, comment_count=row.comment_count,
                share_count=row.share_count, collected_count=row.collected_count,
            )
            upsert_snapshot(s, aid, snap_src, now.date(), now)
            row.last_tracked_at = now
            if row.sec_uid:
                c = s.get(DouyinFinanceCreator, row.sec_uid)
                if c is None:
                    c = DouyinFinanceCreator(
                        sec_uid=row.sec_uid, author_uid=row.author_uid, nickname=row.nickname,
                        follower_count=row.follower_count, flagged_video_count=0, first_seen_at=now,
                        enabled=True, added_by="auto",
                    )
                    s.add(c)
                c.flagged_video_count = (c.flagged_video_count or 0) + 1
                c.last_flagged_at = now

    # -------------------------------------------------------------- 入口
    def run(self, now: Optional[datetime] = None) -> DiscoveryStats:
        now = now or datetime.now()
        stats = DiscoveryStats()
        t0 = time.time()
        records = self.collect(now, stats)
        new_ids = self.store_new(records, now, stats)
        self.classify(new_ids, now, stats)
        logger.info(
            f"[发现] 完成，用时 {time.time() - t0:.0f}s：候选 {stats.candidates}，新增 {stats.new_videos}，"
            f"股票相关 {stats.finance}，标记为上市公司分析 {stats.flagged}，失败 {stats.failed}"
        )
        return stats
