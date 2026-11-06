# -*- coding: utf-8 -*-
"""
每日任务结束后清理不再需要的文件和数据，节省硬盘空间。

会删除：
- 视频/图片临时下载目录（正常每条处理完就删，中断时会残留）
- 抖音登录浏览器的缓存（网页缓存、着色器缓存、崩溃报告、统计文件）；登录信息（Cookies 等）保留
- 超过 STOCK_VIDEO_LOG_KEEP_DAYS 天的每日运行日志
- MediaCrawler 搜索原始数据中超过 STOCK_VIDEO_RAW_KEEP_DAYS 天的记录（识别结果已存到监控表）
- 超过 STOCK_VIDEO_UNFLAGGED_KEEP_DAYS 天、且从未被标记跟踪的候选作品记录

不会删除：被标记跟踪过的作品及其每日数据、预警记录、知识库、每日跟踪表、抖音登录状态。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from loguru import logger
from sqlalchemy import delete, inspect, select, text

from .db import session_scope
from .models import DouyinStockVideo, DouyinStockVideoCompany, DouyinStockVideoSnapshot
from .settings import MINDSPIDER_ROOT, PROJECT_ROOT, MonitorSettings, get_settings

BROWSER_DATA = MINDSPIDER_ROOT / "DeepSentimentCrawling" / "MediaCrawler" / "browser_data"
# 浏览器配置目录里可安全删除的缓存（相对每个 *_user_data_dir）；Cookies / Local Storage 等登录信息不在其中
BROWSER_CACHE_PATHS = [
    "Default/Cache", "Default/Code Cache", "Default/GPUCache", "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache", "Default/Service Worker/CacheStorage", "Default/Service Worker/ScriptCache",
    "GrShaderCache", "GraphiteDawnCache", "ShaderCache", "BrowserMetrics", "Crashpad/reports",
    "BrowserMetrics-spare.pma", "CrashpadMetrics-active.pma",
]


@dataclass
class CleanupStats:
    freed_mb: float = 0.0
    deleted_files: int = 0
    deleted_rows: int = 0
    errors: List[str] = field(default_factory=list)


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _remove(path: Path, stats: CleanupStats) -> None:
    if not path.exists():
        return
    try:
        size = _size(path)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink()
        stats.freed_mb += (size - (_size(path) if path.exists() else 0)) / 1e6
        stats.deleted_files += 1
    except OSError as e:  # 文件被占用等，下次再删
        stats.errors.append(f"{path}: {e}")


def clean_media_cache(s: MonitorSettings, stats: CleanupStats) -> None:
    if s.STOCK_VIDEO_KEEP_MEDIA:
        return
    base = Path(s.STOCK_VIDEO_MEDIA_DIR)
    if base.is_dir():
        for child in base.iterdir():
            _remove(child, stats)


def clean_browser_cache(stats: CleanupStats, root: Path = BROWSER_DATA) -> None:
    if not root.is_dir():
        return
    for profile in root.iterdir():
        if profile.is_dir():
            for rel in BROWSER_CACHE_PATHS:
                _remove(profile / rel, stats)


def clean_logs(s: MonitorSettings, stats: CleanupStats, log_dir: Optional[Path] = None) -> None:
    log_dir = log_dir or PROJECT_ROOT / "logs"
    if not log_dir.is_dir():
        return
    cutoff = time.time() - s.STOCK_VIDEO_LOG_KEEP_DAYS * 86400
    for p in log_dir.glob("stock_video_*.log"):
        if p.stat().st_mtime < cutoff:
            _remove(p, stats)


def clean_database(s: MonitorSettings, now: datetime, stats: CleanupStats) -> None:
    with session_scope() as sess:  # 与监控表使用同一个数据库连接
        engine = sess.get_bind()
    tables = set(inspect(engine).get_table_names())
    # MediaCrawler 原始搜索结果：只用于当天读回，旧的没有用
    raw_cutoff_ms = int((now - timedelta(days=s.STOCK_VIDEO_RAW_KEEP_DAYS)).timestamp() * 1000)
    for table in ("douyin_aweme", "douyin_aweme_comment"):
        if table in tables:
            with engine.begin() as conn:
                stats.deleted_rows += conn.execute(
                    text(f"DELETE FROM {table} WHERE add_ts < :c"), {"c": raw_cutoff_ms}
                ).rowcount or 0
    # 从未标记跟踪的候选作品：超过保留期后不会再被搜到，删除也不影响去重
    cutoff = now - timedelta(days=s.STOCK_VIDEO_UNFLAGGED_KEEP_DAYS)
    with session_scope() as sess:
        ids = list(sess.scalars(select(DouyinStockVideo.aweme_id).where(
            DouyinStockVideo.discovered_at < cutoff,
            DouyinStockVideo.tracking_status == "none",
            DouyinStockVideo.is_tracked.is_(False),
        )))
        for i in range(0, len(ids), 500):
            batch = ids[i:i + 500]
            sess.execute(delete(DouyinStockVideoSnapshot).where(DouyinStockVideoSnapshot.aweme_id.in_(batch)))
            sess.execute(delete(DouyinStockVideoCompany).where(DouyinStockVideoCompany.aweme_id.in_(batch)))
            sess.execute(delete(DouyinStockVideo).where(DouyinStockVideo.aweme_id.in_(batch)))
        stats.deleted_rows += len(ids)


def run_cleanup(now: Optional[datetime] = None, settings: Optional[MonitorSettings] = None) -> CleanupStats:
    s = settings or get_settings()
    now = now or datetime.now()
    stats = CleanupStats()
    for step in (lambda: clean_media_cache(s, stats), lambda: clean_browser_cache(stats),
                 lambda: clean_logs(s, stats), lambda: clean_database(s, now, stats)):
        try:
            step()
        except Exception as e:  # 清理失败不影响每日任务
            stats.errors.append(str(e))
    logger.info(f"[清理] 释放约 {stats.freed_mb:.1f} MB，删除 {stats.deleted_files} 个文件/目录、"
                f"{stats.deleted_rows} 条旧数据" + (f"；{len(stats.errors)} 项未删除（下次再试）" if stats.errors else ""))
    for e in stats.errors[:5]:
        logger.debug(f"[清理] {e}")
    return stats
