# -*- coding: utf-8 -*-
"""每日任务编排（抖音、视频号）：跟踪 → 发现与识别 → 预警 → 通知 → 各平台跟踪表。"""

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
from .sources import SourceError, build_discovery, build_tracking, build_wechat
from .sources.base import PLATFORM_CN
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

    platforms = [p for p in dict.fromkeys(x.lower() for x in s.STOCK_VIDEO_PLATFORMS) if p in ("douyin", "wechat")]
    trackers: dict = {}
    for p in platforms:
        try:
            trackers[p] = build_tracking(s) if p == "douyin" else build_wechat(s)
        except SourceError as e:
            logger.error(f"[{PLATFORM_CN[p]}] {e}；本次不处理该平台")

    # 先跟踪：只需批量查询，几分钟就能完成，保证每天的互动数据不因识别耗时而缺失
    unavailable: dict = {p: [] for p in platforms}
    for p, tracking in trackers.items():
        if skip_tracking:
            break
        tstats = DailyTracker(tracking, s, platform=p).run(now)
        summary[f"tracking_{p}"] = asdict(tstats)
        unavailable[p] = tstats.unavailable_ids

    # 跟踪完先出一版跟踪表：万一后面的识别被中断，当天也有最新的互动数据；全部结束后会再更新一次
    from .report import generate_report

    for p in platforms:
        try:
            generate_report(now.date(), platform=p)
        except Exception as e:
            logger.warning(f"[跟踪表·{PLATFORM_CN[p]}] 预生成失败：{e}")

    # 再发现：视频号只识别文案和封面，很快，先做；抖音要下载解析视频，耗时长，放在后面
    if not skip_discovery:
        matcher = llm = None
        for p in sorted(trackers, key=lambda x: x != "wechat"):
            if hard_deadline and datetime.now() >= hard_deadline:
                logger.warning(f"[每日任务] 已接近截止时间，本次不做{PLATFORM_CN[p]}的发现")
                continue
            if matcher is None:
                matcher, llm = load_matcher(), OpenAICompatLLM(s)
            classifier = TwoStageClassifier(matcher, llm, s.STOCK_VIDEO_PREFILTER_BATCH, s.STOCK_VIDEO_MIN_CONFIDENCE)
            source = build_discovery(s) if p == "douyin" else trackers[p]
            disc = DailyDiscovery(source, classifier, MediaAnalyzer(s), trackers[p], s, platform=p)
            summary[f"discovery_{p}"] = asdict(disc.run(now, hard_deadline))

    for p in platforms:
        engine = AlertEngine(s, platform=p)
        new_alerts = engine.save(engine.evaluate(now.date(), unavailable.get(p, [])))
        notify(new_alerts, s)
        summary[f"alerts_{p}"] = len(new_alerts)
        try:  # 跟踪表生成失败不影响每日任务本身
            summary[f"report_{p}"] = str(generate_report(now.date(), platform=p))
        except Exception as e:
            logger.exception(f"[跟踪表·{PLATFORM_CN[p]}] 生成失败：{e}")
    summary["tikhub_calls"] = {p: t.client.calls for p, t in trackers.items() if hasattr(t, "client")}
    if s.STOCK_VIDEO_CLEANUP:
        from .cleanup import run_cleanup

        summary["cleanup_freed_mb"] = round(run_cleanup(now, s).freed_mb, 1)
    if finish_by:
        summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
    return summary
