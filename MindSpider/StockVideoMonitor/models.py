# -*- coding: utf-8 -*-
"""
抖音 A 股上市公司分析视频监控 —— 数据表

表名沿用「平台_类型」规则，InsightEngine 可直接查询：
- a_share_company              A 股上市公司知识库（名称、简称、产品、技术、品牌）
- douyin_stock_video           财经相关作品主表，含视频和图文（含解析结果、是否重点跟踪）
- douyin_stock_video_company   视频与上市公司的关联（识别依据、观点）
- douyin_stock_video_snapshot  每日互动数据快照
- douyin_finance_creator       财经博主池
- stock_video_alert            预警记录

这些表使用独立的 MonitorBase，与 MediaCrawler 表无外键依赖，可单独建表。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any, List, Optional

from sqlalchemy import BigInteger, Boolean, Date, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class MonitorBase(DeclarativeBase):
    pass


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def loads_list(value: Optional[str]) -> List[Any]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except (TypeError, ValueError):
        return []


class AShareCompany(MonitorBase):
    __tablename__ = "a_share_company"

    stock_code: Mapped[str] = mapped_column(String(10), primary_key=True)  # 600519
    name: Mapped[str] = mapped_column(String(64), index=True)  # 贵州茅台
    exchange: Mapped[Optional[str]] = mapped_column(String(8))  # SH / SZ / BJ
    industry: Mapped[Optional[str]] = mapped_column(String(128))
    main_business: Mapped[Optional[str]] = mapped_column(Text)
    aliases: Mapped[Optional[str]] = mapped_column(Text)  # JSON 数组：简称、曾用名、常见叫法
    products: Mapped[Optional[str]] = mapped_column(Text)  # JSON 数组：主要产品
    technologies: Mapped[Optional[str]] = mapped_column(Text)  # JSON 数组：核心技术
    brands: Mapped[Optional[str]] = mapped_column(Text)  # JSON 数组：品牌
    manual_locked: Mapped[bool] = mapped_column(Boolean, default=False)  # 人工维护过，自动更新不覆盖别名
    enriched_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class DouyinStockVideo(MonitorBase):
    __tablename__ = "douyin_stock_video"
    __table_args__ = (
        Index("idx_dsv_tracking", "is_tracked", "tracking_status"),
        Index("idx_dsv_create_time", "create_time"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    aweme_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[Optional[str]] = mapped_column(Text)
    desc: Mapped[Optional[str]] = mapped_column(Text)
    hashtags: Mapped[Optional[str]] = mapped_column(Text)  # JSON 数组
    create_time: Mapped[Optional[int]] = mapped_column(BigInteger)  # 发布时间，秒级时间戳
    author_uid: Mapped[Optional[str]] = mapped_column(String(64))
    sec_uid: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    nickname: Mapped[Optional[str]] = mapped_column(String(255))
    follower_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    aweme_url: Mapped[Optional[str]] = mapped_column(Text)
    video_url: Mapped[Optional[str]] = mapped_column(Text)
    cover_url: Mapped[Optional[str]] = mapped_column(Text)
    duration_ms: Mapped[Optional[int]] = mapped_column(Integer)
    aweme_type: Mapped[Optional[int]] = mapped_column(Integer)  # 0 视频；2 / 68 / 150 图文
    is_image_post: Mapped[bool] = mapped_column(Boolean, default=False)
    image_urls: Mapped[Optional[str]] = mapped_column(Text)  # JSON 数组，图文作品的图片地址

    discovered_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    discovery_source: Mapped[Optional[str]] = mapped_column(String(32))  # tikhub / mediacrawler
    discovery_channels: Mapped[Optional[str]] = mapped_column(Text)  # JSON 数组：keyword:xx / creator / hot:xx

    # 识别流程：new → prefilter_rejected | finance → analyzed | failed
    stage: Mapped[str] = mapped_column(String(32), default="new", index=True)
    finance_related: Mapped[Optional[bool]] = mapped_column(Boolean)
    asr_text: Mapped[Optional[str]] = mapped_column(Text)
    ocr_text: Mapped[Optional[str]] = mapped_column(Text)
    is_company_analysis: Mapped[Optional[bool]] = mapped_column(Boolean)
    analysis_confidence: Mapped[Optional[float]] = mapped_column(Float)
    analysis_summary: Mapped[Optional[str]] = mapped_column(Text)
    analysis_raw: Mapped[Optional[str]] = mapped_column(Text)  # 大模型原始输出，便于复核
    error_message: Mapped[Optional[str]] = mapped_column(Text)

    # 跟踪：none | active | expired | unavailable
    is_tracked: Mapped[bool] = mapped_column(Boolean, default=False)
    tracking_status: Mapped[str] = mapped_column(String(16), default="none")
    track_until: Mapped[Optional[datetime]] = mapped_column(DateTime)
    miss_count: Mapped[int] = mapped_column(Integer, default=0)
    last_tracked_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    liked_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    comment_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    share_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    collected_count: Mapped[Optional[int]] = mapped_column(BigInteger)


class DouyinStockVideoCompany(MonitorBase):
    __tablename__ = "douyin_stock_video_company"
    __table_args__ = (UniqueConstraint("aweme_id", "stock_code", name="uq_dsvc_video_stock"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    aweme_id: Mapped[str] = mapped_column(String(64), index=True)
    stock_code: Mapped[str] = mapped_column(String(10), index=True)
    stock_name: Mapped[Optional[str]] = mapped_column(String(64))
    basis: Mapped[Optional[str]] = mapped_column(String(16))  # name / product / technology / code
    stance: Mapped[Optional[str]] = mapped_column(String(16))  # bullish / bearish / neutral
    evidence: Mapped[Optional[str]] = mapped_column(Text)
    confidence: Mapped[Optional[float]] = mapped_column(Float)
    video_create_time: Mapped[Optional[int]] = mapped_column(BigInteger, index=True)


class DouyinStockVideoSnapshot(MonitorBase):
    __tablename__ = "douyin_stock_video_snapshot"
    __table_args__ = (UniqueConstraint("aweme_id", "snapshot_date", name="uq_dsvs_video_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    aweme_id: Mapped[str] = mapped_column(String(64), index=True)
    snapshot_date: Mapped[date] = mapped_column(Date, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime)
    liked_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    comment_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    share_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    collected_count: Mapped[Optional[int]] = mapped_column(BigInteger)


class DouyinFinanceCreator(MonitorBase):
    __tablename__ = "douyin_finance_creator"

    sec_uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    author_uid: Mapped[Optional[str]] = mapped_column(String(64))
    nickname: Mapped[Optional[str]] = mapped_column(String(255))
    follower_count: Mapped[Optional[int]] = mapped_column(BigInteger)
    flagged_video_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_flagged_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_fetched_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    added_by: Mapped[Optional[str]] = mapped_column(String(16))  # auto / manual


class StockVideoAlert(MonitorBase):
    __tablename__ = "stock_video_alert"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_date: Mapped[date] = mapped_column(Date, index=True)
    level: Mapped[str] = mapped_column(String(16))  # video / company
    alert_type: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16), default="medium")  # low / medium / high
    aweme_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    stock_code: Mapped[Optional[str]] = mapped_column(String(10), index=True)
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[Optional[str]] = mapped_column(Text)  # JSON
    dedupe_key: Mapped[str] = mapped_column(String(191), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
