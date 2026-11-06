# -*- coding: utf-8 -*-
"""
每日跟踪表（Excel）

每次每日任务结束后自动生成，也可手动运行：python -m StockVideoMonitor.main report

文件：E:\\BettaFishApp\\reports\\抖音上市公司分析视频跟踪_YYYYMMDD.xlsx
      只保留一份表格，包含全部历史数据；每次运行后更新内容，并把文件名改为当天日期。

工作表：
- 跟踪总表：每个「公司 × 作品」一行，按公司名称（拼音）排列，含最新点赞/评论/转发/收藏及较上次检查的增量
- 每日数据：每条作品每天检查一次的数据明细，便于看趋势
- 公司汇总：每家公司的作品数、观点分布、互动总量和当日增量
- 预警记录：全部预警
- 说明
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import select

from .alerts import METRICS, daily_deltas
from .db import session_scope
from .models import DouyinStockVideo, DouyinStockVideoCompany, DouyinStockVideoSnapshot, StockVideoAlert, loads_list
from .settings import PROJECT_ROOT, get_settings

REPORT_DIR = PROJECT_ROOT / "reports"
REPORT_PREFIX = "抖音上市公司分析视频跟踪"


def report_name(day: date) -> str:
    return f"{REPORT_PREFIX}_{day:%Y%m%d}.xlsx"

STANCE_CN = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}
BASIS_CN = {"name": "名称", "code": "代码", "product": "产品", "technology": "技术", "brand": "品牌"}
STATUS_CN = {"active": "跟踪中", "expired": "已到期", "unavailable": "已不可见", "none": "-"}
SEVERITY_CN = {"high": "高", "medium": "中", "low": "低"}
LEVEL_CN = {"video": "作品", "company": "公司"}
ALERT_TYPE_CN = {
    "first_day_burst": "首日爆发", "spike": "互动异动", "unavailable": "作品不可见",
    "video_surge": "讨论激增", "interaction_surge": "互动激增", "stance_shift": "观点反转",
}


def company_sort_key(name: str) -> bytes:
    """按拼音排序：GB2312 一级汉字按拼音编码，用 GBK 字节序近似拼音顺序（无需额外依赖）。"""
    return (name or "").encode("gbk", errors="replace")


def _fmt_ts(ts: Optional[int]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts) if ts else None


def _clip(text: Optional[str], n: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


@dataclass
class VideoMetrics:
    latest: Dict[str, Optional[int]]
    delta: Dict[str, Optional[float]]  # 较上一次检查的日均增量，只有一次快照时为 None
    last_date: Optional[date]
    daily: List[Tuple[date, Dict[str, Optional[int]], Optional[Dict[str, float]]]]


def _metrics(snaps: List[DouyinStockVideoSnapshot]) -> VideoMetrics:
    snaps = sorted(snaps, key=lambda s: s.snapshot_date)
    series = [(s.snapshot_date, {m: getattr(s, m) for m in METRICS}) for s in snaps]
    deltas = daily_deltas(series)
    daily = [(d, vals, (deltas[i][1] if i > 0 else None)) for i, (d, vals) in enumerate(series)]
    if not series:
        return VideoMetrics({m: None for m in METRICS}, {m: None for m in METRICS}, None, [])
    last_delta = deltas[-1][1] if len(series) > 1 else {m: None for m in METRICS}
    return VideoMetrics(series[-1][1], last_delta, series[-1][0], daily)


def collect(today: date) -> dict:
    with session_scope() as s:
        videos = {v.aweme_id: v for v in s.scalars(select(DouyinStockVideo).where(DouyinStockVideo.is_tracked.is_(True)))}
        ids = list(videos)
        mentions = list(s.scalars(select(DouyinStockVideoCompany).where(DouyinStockVideoCompany.aweme_id.in_(ids)))) if ids else []
        snaps = list(s.scalars(select(DouyinStockVideoSnapshot).where(DouyinStockVideoSnapshot.aweme_id.in_(ids)))) if ids else []
        alerts = list(s.scalars(
            select(StockVideoAlert).where(StockVideoAlert.alert_date <= today)
            .order_by(StockVideoAlert.alert_date.desc(), StockVideoAlert.id.desc())
        ))
    by_video: Dict[str, List[DouyinStockVideoSnapshot]] = defaultdict(list)
    for sn in snaps:
        by_video[sn.aweme_id].append(sn)
    return {
        "videos": videos,
        "mentions": mentions,
        "metrics": {aid: _metrics(by_video.get(aid, [])) for aid in videos},
        "alerts": alerts,
    }


# ---------------------------------------------------------------------- Excel 输出
def _styles():
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    thin = Side(style="thin", color="D9D9D9")
    return {
        "header_font": Font(bold=True, color="FFFFFF"),
        "header_fill": PatternFill("solid", fgColor="1F4E78"),
        "band_fill": PatternFill("solid", fgColor="F2F6FA"),
        "alert_fill": PatternFill("solid", fgColor="FDE9E7"),
        "up_font": Font(color="C00000"),       # A 股习惯：上涨/增加用红色
        "link_font": Font(color="0563C1", underline="single"),
        "status_font": {"跟踪中": Font(color="2E7D32"), "已到期": Font(color="808080"), "已不可见": Font(color="E65100")},
        "border": Border(bottom=thin),
        "wrap": Alignment(wrap_text=True, vertical="top"),
        "center": Alignment(horizontal="center", vertical="top"),
        "top": Alignment(vertical="top"),
    }


def _write_sheet(ws, headers: List[Tuple[str, int]], rows: List[list], st, *, number_cols=(), delta_cols=(),
                 link_col: Optional[int] = None, band_key_col: Optional[int] = 0, freeze: str = "C2",
                 highlight_rows: Optional[set] = None, status_col: Optional[int] = None, wrap_cols=()):
    from openpyxl.utils import get_column_letter

    for c, (title, width) in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=title)
        cell.font, cell.fill, cell.alignment = st["header_font"], st["header_fill"], st["center"]
        ws.column_dimensions[get_column_letter(c)].width = width
    ws.row_dimensions[1].height = 22
    band, last_key = False, object()
    for r, row in enumerate(rows, 2):
        if band_key_col is not None and row[band_key_col] != last_key:
            band, last_key = not band, row[band_key_col]
        for c, value in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = st["wrap"] if (c - 1) in wrap_cols else st["top"]
            cell.border = st["border"]
            if highlight_rows and (r - 2) in highlight_rows:
                cell.fill = st["alert_fill"]
            elif band and band_key_col is not None:
                cell.fill = st["band_fill"]
            if (c - 1) in number_cols or (c - 1) in delta_cols:
                cell.number_format = "#,##0"
            if (c - 1) in delta_cols and isinstance(value, (int, float)) and value > 0:
                cell.font = st["up_font"]
                cell.number_format = "+#,##0"
            if status_col is not None and (c - 1) == status_col and value in st["status_font"]:
                cell.font = st["status_font"][value]
            if link_col is not None and (c - 1) == link_col and value:
                cell.hyperlink, cell.value, cell.font = value, "打开", st["link_font"]
            if isinstance(value, datetime):
                cell.number_format = "yyyy-mm-dd hh:mm"
            elif isinstance(value, date):
                cell.number_format = "yyyy-mm-dd"
    ws.freeze_panes = freeze
    # 打印：横向、宽度缩放到一页
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToWidth, ws.page_setup.fitToHeight = 1, 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.print_title_rows = "1:1"
    if rows:
        ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"


def build_workbook(data: dict, today: date, generated_at: datetime):
    from openpyxl import Workbook

    st = _styles()
    videos, metrics = data["videos"], data["metrics"]
    alerted_videos = {a.aweme_id for a in data["alerts"] if a.alert_date == today and a.aweme_id}
    alerted_companies = {a.stock_code for a in data["alerts"] if a.alert_date == today and a.stock_code}
    alert_titles: Dict[str, List[str]] = defaultdict(list)
    for a in data["alerts"]:
        if a.alert_date == today:
            alert_titles[a.aweme_id or a.stock_code or ""].append(ALERT_TYPE_CN.get(a.alert_type, a.alert_type))

    mentions = sorted(
        (m for m in data["mentions"] if m.aweme_id in videos),
        key=lambda m: (company_sort_key(m.stock_name or ""), m.stock_code,
                       -(videos[m.aweme_id].create_time or 0)),
    )

    wb = Workbook()
    # ---- 跟踪总表
    ws = wb.active
    ws.title = "跟踪总表"
    headers = [("公司名称", 12), ("股票代码", 9), ("观点", 6), ("识别依据", 8), ("作品标题/文案", 42), ("类型", 6),
               ("博主", 14), ("粉丝数", 11), ("发布时间", 16), ("标记日期", 11), ("跟踪状态", 9), ("已跟踪天数", 9),
               ("最近检查", 11), ("点赞", 10), ("评论", 9), ("转发", 9), ("收藏", 9),
               ("点赞日增", 10), ("评论日增", 9), ("转发日增", 9), ("收藏日增", 9),
               ("今日预警", 14), ("原文证据", 36), ("作品链接", 8)]
    rows, highlight = [], set()
    track_days = get_settings().STOCK_VIDEO_TRACK_DAYS
    for m in mentions:
        v, mt = videos[m.aweme_id], metrics[m.aweme_id]
        flagged = v.track_until - timedelta(days=track_days) if v.track_until else None
        tracked_days = (min(today, (v.track_until or datetime.max).date()) - flagged.date()).days + 1 if flagged else None
        warn = "、".join(alert_titles.get(v.aweme_id, []) + alert_titles.get(m.stock_code, []))
        if v.aweme_id in alerted_videos or m.stock_code in alerted_companies:
            highlight.add(len(rows))
        rows.append([
            m.stock_name, m.stock_code, STANCE_CN.get(m.stance or "", m.stance), BASIS_CN.get(m.basis or "", m.basis),
            _clip(v.desc or v.title, 120), "图文" if v.is_image_post else "视频", v.nickname, v.follower_count,
            _fmt_ts(v.create_time), flagged.date() if flagged else None, STATUS_CN.get(v.tracking_status, v.tracking_status),
            tracked_days, mt.last_date,
            mt.latest["liked_count"], mt.latest["comment_count"], mt.latest["share_count"], mt.latest["collected_count"],
            *(round(mt.delta[k]) if mt.delta[k] is not None else None
              for k in ("liked_count", "comment_count", "share_count", "collected_count")),
            warn or None, _clip(m.evidence, 120), v.aweme_url,
        ])
    _write_sheet(ws, headers, rows, st, number_cols={7, 13, 14, 15, 16}, delta_cols={17, 18, 19, 20},
                 link_col=23, highlight_rows=highlight, status_col=10, wrap_cols={4, 22})

    # ---- 每日数据
    ws2 = wb.create_sheet("每日数据")
    h2 = [("公司名称", 12), ("股票代码", 9), ("作品", 34), ("博主", 14), ("检查日期", 11),
          ("点赞", 10), ("评论", 9), ("转发", 9), ("收藏", 9),
          ("点赞日增", 10), ("评论日增", 9), ("转发日增", 9), ("收藏日增", 9), ("作品链接", 8)]
    rows2 = []
    for m in mentions:
        v, mt = videos[m.aweme_id], metrics[m.aweme_id]
        for d, vals, delta in mt.daily:
            rows2.append([
                m.stock_name, m.stock_code, _clip(v.desc or v.title, 40), v.nickname, d,
                vals["liked_count"], vals["comment_count"], vals["share_count"], vals["collected_count"],
                *((round(delta[k]) if delta else None) for k in ("liked_count", "comment_count", "share_count", "collected_count")),
                v.aweme_url,
            ])
    _write_sheet(ws2, h2, rows2, st, number_cols={5, 6, 7, 8}, delta_cols={9, 10, 11, 12}, link_col=13)

    # ---- 公司汇总
    ws3 = wb.create_sheet("公司汇总")
    agg: Dict[str, dict] = {}
    for m in mentions:
        v, mt = videos[m.aweme_id], metrics[m.aweme_id]
        a = agg.setdefault(m.stock_code, {"name": m.stock_name, "active": 0, "today": 0, "bullish": 0, "bearish": 0,
                                          "neutral": 0, "liked": 0, "collected": 0, "shared": 0,
                                          "d_liked": 0, "d_collected": 0, "d_shared": 0})
        if v.tracking_status == "active":
            a["active"] += 1
        if v.discovered_at and v.discovered_at.date() == today:
            a["today"] += 1
        a[m.stance if m.stance in ("bullish", "bearish", "neutral") else "neutral"] += 1
        a["liked"] += mt.latest["liked_count"] or 0
        a["collected"] += mt.latest["collected_count"] or 0
        a["shared"] += mt.latest["share_count"] or 0
        if mt.last_date == today:
            a["d_liked"] += round(mt.delta["liked_count"] or 0)
            a["d_collected"] += round(mt.delta["collected_count"] or 0)
            a["d_shared"] += round(mt.delta["share_count"] or 0)
    h3 = [("公司名称", 12), ("股票代码", 9), ("跟踪中作品", 10), ("今日新增作品", 11), ("看多", 6), ("看空", 6), ("中性", 6),
          ("累计点赞", 11), ("累计收藏", 10), ("累计转发", 10), ("今日点赞增量", 12), ("今日收藏增量", 12),
          ("今日转发增量", 12), ("今日预警", 16)]
    rows3, hl3 = [], set()
    for code, a in sorted(agg.items(), key=lambda kv: (company_sort_key(kv[1]["name"] or ""), kv[0])):
        if code in alerted_companies:
            hl3.add(len(rows3))
        rows3.append([a["name"], code, a["active"], a["today"], a["bullish"], a["bearish"], a["neutral"],
                      a["liked"], a["collected"], a["shared"], a["d_liked"] or None, a["d_collected"] or None,
                      a["d_shared"] or None, "、".join(alert_titles.get(code, [])) or None])
    _write_sheet(ws3, h3, rows3, st, number_cols={2, 3, 4, 5, 6, 7, 8, 9}, delta_cols={10, 11, 12},
                 band_key_col=None, highlight_rows=hl3)

    # ---- 预警记录
    ws4 = wb.create_sheet("预警记录")
    h4 = [("日期", 11), ("级别", 6), ("类型", 10), ("严重程度", 8), ("股票代码", 9), ("内容", 80), ("作品链接", 8)]
    rows4 = [[a.alert_date, LEVEL_CN.get(a.level, a.level), ALERT_TYPE_CN.get(a.alert_type, a.alert_type),
              SEVERITY_CN.get(a.severity, a.severity), a.stock_code,
              a.title, videos[a.aweme_id].aweme_url if a.aweme_id in videos else None] for a in data["alerts"]]
    _write_sheet(ws4, h4, rows4, st, link_col=6, band_key_col=None, freeze="A2", wrap_cols={5})

    # ---- 说明
    ws5 = wb.create_sheet("说明")
    notes = [
        f"更新时间：{generated_at:%Y-%m-%d %H:%M}（每日任务结束后自动更新）",
        f"跟踪中的作品：{sum(1 for v in videos.values() if v.tracking_status == 'active')} 条；涉及公司：{len(agg)} 家",
        "",
        "跟踪总表：每个「公司 × 作品」一行，按公司名称拼音排序；一条作品涉及多家公司时会出现在每家公司下。",
        "点赞/评论/转发/收藏：最近一次检查时的累计数；「日增」为较上一次检查的增量（隔了几天则按天平均），首次检查为空。",
        "红色数字表示增加；浅红底色的行今天触发了预警。",
        "跟踪状态：跟踪中＝标记后 14 天内每天检查；已到期＝满 14 天停止；已不可见＝作品被删除或设为私密。",
        "观点：大模型根据作品内容判断的看多/看空/中性；识别依据：通过公司名称、股票代码、产品或技术识别。",
        "每日数据：每条作品每天的检查记录，可用筛选按公司或作品查看趋势。",
        "本表只有一份，包含全部历史数据；每天运行后更新内容，文件名中的日期即数据截至日期。",
    ]
    ws5.column_dimensions["A"].width = 110
    for i, line in enumerate(notes, 1):
        ws5.cell(row=i, column=1, value=line).alignment = st["wrap"]
    return wb


def generate_report(today: Optional[date] = None, out_dir: Optional[Path] = None) -> Path:
    """
    生成（更新）唯一的跟踪表：写入以当天日期命名的新文件，再删除旧日期的那份，
    相当于「更新内容 + 改名」。旧文件若正被 Excel 打开而删不掉，下次运行再清理。
    """
    today = today or date.today()
    out_dir = Path(out_dir or REPORT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    wb = build_workbook(collect(today), today, datetime.now())
    target = out_dir / report_name(today)
    try:
        wb.save(target)
    except PermissionError:  # 当天的表格正在 Excel 中打开（同一天重复运行），换个文件名保存
        target = out_dir / f"{REPORT_PREFIX}_{today:%Y%m%d}_{datetime.now():%H%M}.xlsx"
        wb.save(target)
        logger.warning(f"[跟踪表] 当天的表格正被打开，已另存为 {target.name}")
    for old in out_dir.glob(f"{REPORT_PREFIX}*.xlsx"):
        if old.resolve() == target.resolve():
            continue
        try:
            old.unlink()
        except OSError:
            logger.warning(f"[跟踪表] 旧表格 {old.name} 正被打开，暂未删除，下次运行时清理")
    logger.info(f"[跟踪表] 已更新：{target}")
    return target


def latest_report(out_dir: Optional[Path] = None) -> Optional[Path]:
    files = sorted(Path(out_dir or REPORT_DIR).glob(f"{REPORT_PREFIX}*.xlsx"), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None
