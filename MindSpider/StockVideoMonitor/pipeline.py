# -*- coding: utf-8 -*-
"""每日任务编排：跟踪 → 发现与识别 → 预警 → 通知 → 跟踪表。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger

from .alerts import AlertEngine
from .classifier import TwoStageClassifier
from .discovery import DailyDiscovery
from .knowledge_base import load_matcher
from .llm import OpenAICompatLLM
from .media import MediaAnalyzer
from .notifiers import notify
from .settings import MonitorSettings, get_settings
from .sources import SourceError, build_discovery, build_tracking
from .tracker import DailyTracker


def finish_deadline(now: datetime, s: MonitorSettings) -> Optional[datetime]:
    """当天必须结束的时间（如 06:50）；配置为空则不限。"""
    text = (s.STOCK_VIDEO_FINISH_BY or "").strip()
    if not text:
        return None
    h, m = (int(x) for x in text.split(":"))
    return now.replace(hour=h, minute=m, second=0, microsecond=0)


def run_daily(now: Optional[datetime] = None, settings: Optional[MonitorSettings] = None,
              skip_discovery: bool = False, skip_tracking: bool = False,
              enforce_deadline: bool = False) -> dict:
    """每日任务。enforce_deadline=True（计划任务）时：
    晚于截止时间启动就直接跳过；先跟踪再发现；识别在截止前预留时间收尾，剩余作品留到下次。"""
    s = settings or get_settings()
    now = now or datetime.now()
    summary: dict = {"run_at": now.isoformat(timespec="seconds")}

    finish_by = finish_deadline(now, s) if enforce_deadline else None
    if finish_by and now >= finish_by:
        logger.warning(f"[每日任务] 启动时间 {now:%H:%M} 已晚于截止时间 {finish_by:%H:%M}，"
                       f"为不影响电脑上的其他任务，本次跳过；下次运行会自动补抓漏掉的时段")
        summary["skipped"] = "late_start"
        return summary
    hard_deadline = finish_by - timedelta(minutes=s.STOCK_VIDEO_RESERVE_MINUTES) if finish_by else None

    try:
        tracking = build_tracking(s)
    except SourceError as e:
        logger.error(f"{e}；本次只做发现，不做跟踪")
        tracking = None

    # 先跟踪：只需批量查询，几分钟就能完成，保证每天的互动数据不因识别耗时而缺失
    unavailable = []
    if tracking is not None and not skip_tracking:
        tstats = DailyTracker(tracking, s).run(now)
        summary["tracking"] = asdict(tstats)
        unavailable = tstats.unavailable_ids

    if not skip_discovery:
        if hard_deadline and datetime.now() >= hard_deadline:
            logger.warning("[每日任务] 跟踪结束时已接近截止时间，本次不做发现")
        else:
            matcher = load_matcher()
            llm = OpenAICompatLLM(s)
            classifier = TwoStageClassifier(matcher, llm, s.STOCK_VIDEO_PREFILTER_BATCH, s.STOCK_VIDEO_MIN_CONFIDENCE)
            disc = DailyDiscovery(build_discovery(s), classifier, MediaAnalyzer(s), tracking, s)
            summary["discovery"] = asdict(disc.run(now, hard_deadline))

    engine = AlertEngine(s)
    new_alerts = engine.save(engine.evaluate(now.date(), unavailable))
    notify(new_alerts, s)
    summary["alerts"] = len(new_alerts)
    try:  # 跟踪表生成失败不影响每日任务本身
        from .report import generate_report

        summary["report"] = str(generate_report(now.date()))
    except Exception as e:
        logger.exception(f"[跟踪表] 生成失败：{e}")
    if tracking is not None and hasattr(tracking, "client"):
        summary["tikhub_calls"] = tracking.client.calls
    if s.STOCK_VIDEO_CLEANUP:
        from .cleanup import run_cleanup

        summary["cleanup_freed_mb"] = round(run_cleanup(now, s).freed_mb, 1)
    if finish_by:
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return summary
