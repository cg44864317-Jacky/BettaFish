# -*- coding: utf-8 -*-
"""
通知渠道

目前只实现 log（写日志 + 生成当日预警汇总文本）。企业微信、飞书、钉钉、邮件等渠道
确定后，只需继承 Notifier 实现 send()，并在 NOTIFIERS 里注册名称。
"""

from __future__ import annotations

from typing import Dict, List, Optional, Type

from loguru import logger
from sqlalchemy import update

from .alerts import Alert
from .db import session_scope
from .models import StockVideoAlert
from .settings import MonitorSettings, get_settings

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}
SEVERITY_CN = {"high": "高", "medium": "中", "low": "低"}


def format_digest(alerts: List[Alert]) -> str:
    if not alerts:
        return "今日无预警。"
    lines = [f"抖音上市公司分析视频监控 · {alerts[0].alert_date} 预警 {len(alerts)} 条"]
    for a in sorted(alerts, key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.level != "company")):
        url = a.detail.get("aweme_url") if a.detail else None
        lines.append(f"[{SEVERITY_CN.get(a.severity, a.severity)}] {a.title}" + (f"\n    {url}" if url else ""))
    return "\n".join(lines)


class Notifier:
    name = "base"

    def send(self, alerts: List[Alert]) -> None:
        raise NotImplementedError


class LogNotifier(Notifier):
    name = "log"

    def send(self, alerts: List[Alert]) -> None:
        logger.info("\n" + format_digest(alerts))


NOTIFIERS: Dict[str, Type[Notifier]] = {"log": LogNotifier}


def notify(alerts: List[Alert], settings: Optional[MonitorSettings] = None) -> None:
    if not alerts:
        return
    s = settings or get_settings()
    ok = False
    for name in s.STOCK_VIDEO_NOTIFIERS:
        cls = NOTIFIERS.get(name)
        if cls is None:
            logger.warning(f"[通知] 未知渠道 {name}，已跳过")
            continue
        try:
            cls().send(alerts)
            ok = True
        except Exception as e:
            logger.exception(f"[通知] {name} 发送失败：{e}")
    if ok:
        with session_scope() as sess:
            sess.execute(
                update(StockVideoAlert)
                .where(StockVideoAlert.dedupe_key.in_([a.dedupe_key for a in alerts]))
                .values(notified=True)
            )
