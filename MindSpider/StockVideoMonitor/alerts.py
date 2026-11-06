# -*- coding: utf-8 -*-
"""
异常预警（按日）

视频级：
- first_day_burst  新视频首次快照点赞就超过阈值
- spike            某项指标当日增量 ≥ 自身此前日均增量 × 倍数，且超过最小增量（过滤小数波动）
- unavailable      跟踪中的视频被删除 / 设为私密

公司级（同一家公司的所有分析视频汇总）：
- video_surge        当日新增分析视频数 ≥ 阈值，且 ≥ 过去 7 日日均 × 倍数
- interaction_surge  当日互动增量（点赞+转发+收藏）≥ 阈值，且 ≥ 过去 7 日日均 × 倍数
- stance_shift       当日看空占比与过去 7 日相比变化超过阈值（观点反转）
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from loguru import logger
from sqlalchemy import func, select

from .db import session_scope
from .models import DouyinStockVideo, DouyinStockVideoCompany, DouyinStockVideoSnapshot, StockVideoAlert, dumps, loads_list
from .settings import MonitorSettings, get_settings

METRICS = ("liked_count", "share_count", "collected_count", "comment_count")
METRIC_CN = {"liked_count": "点赞", "share_count": "转发", "collected_count": "收藏", "comment_count": "评论"}
STANCE_CN = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}


@dataclass
class Alert:
    alert_date: date
    level: str
    alert_type: str
    title: str
    severity: str = "medium"
    aweme_id: Optional[str] = None
    stock_code: Optional[str] = None
    detail: dict = field(default_factory=dict)

    @property
    def dedupe_key(self) -> str:
        return f"{self.alert_date}:{self.level}:{self.alert_type}:{self.aweme_id or self.stock_code or ''}"


Snap = Tuple[date, Dict[str, Optional[int]]]


def daily_deltas(snaps: Sequence[Snap]) -> List[Tuple[date, Dict[str, float]]]:
    """把累计快照转成每日增量；中间缺了几天就平均分摊。第一张快照视为当天的增量。"""
    out: List[Tuple[date, Dict[str, float]]] = []
    prev: Optional[Snap] = None
    for d, vals in sorted(snaps, key=lambda x: x[0]):
        if prev is None:
            out.append((d, {m: float(vals.get(m) or 0) for m in METRICS}))
        else:
            gap = max(1, (d - prev[0]).days)
            out.append((d, {m: max(0.0, float((vals.get(m) or 0) - (prev[1].get(m) or 0))) / gap for m in METRICS}))
        prev = (d, vals)
    return out


class AlertEngine:
    def __init__(self, settings: Optional[MonitorSettings] = None):
        self.s = settings or get_settings()
        self.min_delta = {
            "liked_count": self.s.STOCK_VIDEO_ALERT_MIN_LIKES_DELTA,
            "share_count": self.s.STOCK_VIDEO_ALERT_MIN_SHARES_DELTA,
            "collected_count": self.s.STOCK_VIDEO_ALERT_MIN_COLLECTS_DELTA,
        }

    # -------------------------------------------------------------- 视频级
    def video_alerts(self, video: DouyinStockVideo, snaps: Sequence[Snap], today: date, names: str) -> List[Alert]:
        deltas = daily_deltas(snaps)
        if not deltas or deltas[-1][0] != today:
            return []
        label = f"「{(video.desc or video.title or video.aweme_id)[:30]}」（{video.nickname or '未知博主'}）"
        base = {"aweme_url": video.aweme_url, "companies": names, "nickname": video.nickname}
        if len(deltas) == 1:
            likes = deltas[0][1]["liked_count"]
            if likes >= self.s.STOCK_VIDEO_ALERT_FIRST_DAY_LIKES:
                return [Alert(today, "video", "first_day_burst", f"新视频首日爆发：{label} 点赞 {int(likes):,}，涉及 {names}",
                              "high", video.aweme_id, detail={**base, "likes": int(likes)})]
            return []
        today_d = deltas[-1][1]
        history = [d for _, d in deltas[:-1]]
        spikes = {}
        for m, min_d in self.min_delta.items():
            baseline = sum(h[m] for h in history) / len(history)
            ratio = today_d[m] / baseline if baseline > 0 else float("inf")
            if today_d[m] >= min_d and ratio >= self.s.STOCK_VIDEO_ALERT_SPIKE_RATIO:
                spikes[m] = {"today": int(today_d[m]), "baseline": round(baseline, 1),
                             "ratio": round(ratio, 1) if ratio != float("inf") else None}
        if not spikes:
            return []
        parts = "、".join(f"{METRIC_CN[m]} +{v['today']:,}（日均 {v['baseline']:,.0f}）" for m, v in spikes.items())
        strong = len(spikes) >= 2 or any((v["ratio"] or 1e9) >= 2 * self.s.STOCK_VIDEO_ALERT_SPIKE_RATIO for v in spikes.values())
        return [Alert(today, "video", "spike", f"视频互动异动：{label} {parts}，涉及 {names}",
                      "high" if strong else "medium", video.aweme_id, detail={**base, "spikes": spikes})]

    # -------------------------------------------------------------- 公司级
    def company_alerts(
        self,
        code: str,
        name: str,
        daily_videos: Dict[date, int],
        daily_interaction: Dict[date, float],
        daily_stance: Dict[date, Dict[str, int]],
        today: date,
    ) -> List[Alert]:
        alerts: List[Alert] = []
        past_days = [today - timedelta(days=i) for i in range(1, 8)]
        ratio = self.s.STOCK_VIDEO_ALERT_COMPANY_RATIO

        n_today = daily_videos.get(today, 0)
        avg_v = sum(daily_videos.get(d, 0) for d in past_days) / 7
        if n_today >= self.s.STOCK_VIDEO_ALERT_COMPANY_VIDEOS and n_today >= ratio * max(avg_v, 1):
            alerts.append(Alert(today, "company", "video_surge",
                                f"{name}（{code}）讨论激增：今日新增 {n_today} 条分析视频，过去 7 日日均 {avg_v:.1f} 条",
                                "high" if n_today >= 2 * ratio * max(avg_v, 1) else "medium", stock_code=code,
                                detail={"today": n_today, "avg7": round(avg_v, 2)}))

        i_today = daily_interaction.get(today, 0.0)
        avg_i = sum(daily_interaction.get(d, 0.0) for d in past_days) / 7
        if i_today >= self.s.STOCK_VIDEO_ALERT_COMPANY_MIN_INTERACTION and (avg_i == 0 or i_today >= ratio * avg_i):
            alerts.append(Alert(today, "company", "interaction_surge",
                                f"{name}（{code}）相关视频互动激增：今日新增互动 {int(i_today):,}，过去 7 日日均 {int(avg_i):,}",
                                "medium", stock_code=code, detail={"today": int(i_today), "avg7": int(avg_i)}))

        st_today = daily_stance.get(today, {})
        st_past: Dict[str, int] = defaultdict(int)
        for d in past_days:
            for k, v in daily_stance.get(d, {}).items():
                st_past[k] += v
        n1, n0 = sum(st_today.values()), sum(st_past.values())
        if n1 >= 3 and n0 >= 3:
            bear1, bear0 = st_today.get("bearish", 0) / n1, st_past.get("bearish", 0) / n0
            if abs(bear1 - bear0) >= self.s.STOCK_VIDEO_ALERT_STANCE_SHIFT:
                direction = "转空" if bear1 > bear0 else "转多"
                alerts.append(Alert(today, "company", "stance_shift",
                                    f"{name}（{code}）观点{direction}：今日看空占比 {bear1:.0%}，过去 7 日 {bear0:.0%}（今日 {n1} 条）",
                                    "high", stock_code=code,
                                    detail={"today": dict(st_today), "past7": dict(st_past), "bear_today": round(bear1, 3),
                                            "bear_past7": round(bear0, 3)}))
        return alerts

    # -------------------------------------------------------------- 从数据库计算
    def evaluate(self, today: Optional[date] = None, unavailable_ids: Sequence[str] = ()) -> List[Alert]:
        today = today or date.today()
        window_start = today - timedelta(days=self.s.STOCK_VIDEO_TRACK_DAYS + 8)
        with session_scope() as s:
            videos = {
                v.aweme_id: v for v in s.scalars(
                    select(DouyinStockVideo).where(
                        DouyinStockVideo.is_company_analysis.is_(True),
                        DouyinStockVideo.discovered_at >= datetime.combine(window_start, datetime.min.time()),
                    )
                )
            }
            mentions = list(s.scalars(select(DouyinStockVideoCompany).where(DouyinStockVideoCompany.aweme_id.in_(list(videos))))) if videos else []
            snaps_rows = list(s.scalars(select(DouyinStockVideoSnapshot).where(DouyinStockVideoSnapshot.aweme_id.in_(list(videos))))) if videos else []

        snaps: Dict[str, List[Snap]] = defaultdict(list)
        for r in snaps_rows:
            snaps[r.aweme_id].append((r.snapshot_date, {m: getattr(r, m) for m in METRICS}))
        by_video: Dict[str, List[DouyinStockVideoCompany]] = defaultdict(list)
        for m in mentions:
            by_video[m.aweme_id].append(m)

        alerts: List[Alert] = []
        for aid, v in videos.items():
            names = "、".join(f"{m.stock_name}({m.stock_code})" for m in by_video.get(aid, [])) or "未知公司"
            alerts.extend(self.video_alerts(v, snaps.get(aid, []), today, names))
        for aid in unavailable_ids:
            v = videos.get(aid)
            if v:
                names = "、".join(m.stock_name for m in by_video.get(aid, []))
                alerts.append(Alert(today, "video", "unavailable", f"跟踪中的视频已不可见（可能被删除或隐藏）：{(v.desc or '')[:30]}（{v.nickname}），涉及 {names}",
                                    "low", aid, detail={"aweme_url": v.aweme_url}))

        # 公司维度汇总。运行天数不足时历史基线不可靠（日均接近 0，任何讨论都像「激增」），先不出公司级预警
        with session_scope() as s:
            first_seen = s.scalar(select(func.min(DouyinStockVideo.discovered_at)))
        history_days = (today - first_seen.date()).days if first_seen else 0
        if history_days < self.s.STOCK_VIDEO_ALERT_WARMUP_DAYS:
            logger.info(f"[预警] 已积累 {history_days} 天数据，满 {self.s.STOCK_VIDEO_ALERT_WARMUP_DAYS} 天后才启用公司级预警")
            return alerts
        comp_videos: Dict[str, Dict[date, int]] = defaultdict(lambda: defaultdict(int))
        comp_inter: Dict[str, Dict[date, float]] = defaultdict(lambda: defaultdict(float))
        comp_stance: Dict[str, Dict[date, Dict[str, int]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
        comp_name: Dict[str, str] = {}
        for aid, ms in by_video.items():
            v = videos[aid]
            d0 = v.discovered_at.date() if v.discovered_at else None
            deltas = daily_deltas(snaps.get(aid, []))
            for m in ms:
                comp_name[m.stock_code] = m.stock_name or m.stock_code
                # 只通过「人气榜股票名」搜到的作品是我们主动按名字搜来的，计入讨论量会高估热门股，不计数
                channels = loads_list(v.discovery_channels)
                hot_only = bool(channels) and all(str(c).startswith("hot:") for c in channels)
                if d0 and not hot_only:
                    comp_videos[m.stock_code][d0] += 1
                    comp_stance[m.stock_code][d0][m.stance or "neutral"] += 1
                for d, delta in deltas:
                    comp_inter[m.stock_code][d] += delta["liked_count"] + delta["share_count"] + delta["collected_count"]
        for code in comp_name:
            alerts.extend(self.company_alerts(code, comp_name[code], comp_videos[code], comp_inter[code], comp_stance[code], today))
        return alerts

    @staticmethod
    def save(alerts: List[Alert]) -> List[Alert]:
        """写入数据库并去重，返回本次新产生的预警。"""
        new: List[Alert] = []
        with session_scope() as s:
            existing = set(s.scalars(select(StockVideoAlert.dedupe_key).where(StockVideoAlert.dedupe_key.in_([a.dedupe_key for a in alerts])))) if alerts else set()
            for a in alerts:
                if a.dedupe_key in existing:
                    continue
                existing.add(a.dedupe_key)
                s.add(StockVideoAlert(alert_date=a.alert_date, level=a.level, alert_type=a.alert_type, severity=a.severity,
                                      aweme_id=a.aweme_id, stock_code=a.stock_code, title=a.title, detail=dumps(a.detail),
                                      dedupe_key=a.dedupe_key, created_at=datetime.now(), notified=False))
                new.append(a)
        logger.info(f"[预警] 共 {len(alerts)} 条，新增 {len(new)} 条")
        return new
