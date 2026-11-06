# -*- coding: utf-8 -*-
"""每日任务编排：发现与识别 → 跟踪 → 预警 → 通知。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
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


def run_daily(now: Optional[datetime] = None, settings: Optional[MonitorSettings] = None,
              skip_discovery: bool = False, skip_tracking: bool = False) -> dict:
    s = settings or get_settings()
    now = now or datetime.now()
    summary: dict = {"run_at": now.isoformat(timespec="seconds")}

    try:
        tracking = build_tracking(s)
    except SourceError as e:
        logger.error(f"{e}；本次只做发现，不做跟踪")
        tracking = None

    if not skip_discovery:
        matcher = load_matcher()
        llm = OpenAICompatLLM(s)
        classifier = TwoStageClassifier(matcher, llm, s.STOCK_VIDEO_PREFILTER_BATCH, s.STOCK_VIDEO_MIN_CONFIDENCE)
        disc = DailyDiscovery(build_discovery(s), classifier, MediaAnalyzer(s), tracking, s)
        summary["discovery"] = asdict(disc.run(now))

    unavailable = []
    if tracking is not None and not skip_tracking:
        tstats = DailyTracker(tracking, s).run(now)
        summary["tracking"] = asdict(tstats)
        unavailable = tstats.unavailable_ids

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
    return summary
