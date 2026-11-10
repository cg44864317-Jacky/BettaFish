# -*- coding: utf-8 -*-
"""
命令行入口（在 MindSpider 目录下运行）：

    python -m StockVideoMonitor.main init-db                 # 建表
    python -m StockVideoMonitor.main build-kb                # 构建/更新 A 股知识库（建议每周一次）
    python -m StockVideoMonitor.main run                     # 执行一次每日任务：发现 → 跟踪 → 预警
    python -m StockVideoMonitor.main schedule --at 07:30     # 常驻进程，每天定时执行
    python -m StockVideoMonitor.main track                   # 只跟踪
    python -m StockVideoMonitor.main alerts                  # 只重新计算今日预警
    python -m StockVideoMonitor.main add-creator <sec_uid>   # 手动把博主加入博主池
    python -m StockVideoMonitor.main status                  # 查看监控概况
    python -m StockVideoMonitor.main selftest                # 部署自检
    python -m StockVideoMonitor.main report                  # 重新生成每日跟踪表（Excel）
    python -m StockVideoMonitor.main douyin-login            # 抖音扫码登录（首次或登录失效时）
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, datetime, timedelta

from loguru import logger
from sqlalchemy import func, select

from .db import create_tables, session_scope
from .models import DouyinFinanceCreator, DouyinStockVideo, StockVideoAlert, AShareCompany
from .settings import get_settings


def cmd_init_db(_args) -> None:
    create_tables()
    logger.info("监控模块数据表已创建")


def cmd_build_kb(args) -> None:
    from .knowledge_base import cleanup_knowledge_base, enrich_with_llm, sync_company_list
    from .llm import OpenAICompatLLM

    create_tables()
    changed = sync_company_list(fetch_business=not args.skip_business, only_missing=not args.refresh_business)
    logger.info(f"[知识库] 公司列表同步完成，新增/改名 {changed} 家")
    if not args.skip_llm:
        n = enrich_with_llm(OpenAICompatLLM(), limit=args.enrich_limit, refresh=args.refresh_llm)
        logger.info(f"[知识库] 大模型扩充完成 {n} 家")
    cleanup_knowledge_base()


def cmd_run(args) -> None:
    from .pipeline import run_daily

    create_tables()
    summary = run_daily(skip_discovery=args.skip_discovery, skip_tracking=args.skip_tracking,
                        enforce_deadline=args.scheduled)
    logger.info("本次运行汇总：\n" + json.dumps(summary, ensure_ascii=False, indent=2, default=str))


def cmd_cleanup(_args) -> None:
    from .cleanup import run_cleanup

    create_tables()
    run_cleanup()


def _platforms() -> list:
    from .settings import get_settings

    return [p for p in dict.fromkeys(x.lower() for x in get_settings().STOCK_VIDEO_PLATFORMS) if p in ("douyin", "wechat")]


def cmd_recheck(args) -> None:
    from .recheck import recheck
    from .report import generate_report

    create_tables()
    if args.rejudge:
        from .recheck import rejudge

        rejudge(workers=args.workers, dry_run=args.dry_run)
    else:
        recheck(dry_run=args.dry_run)
    if not args.dry_run:
        for p in _platforms():
            generate_report(platform=p)


def cmd_why(args) -> None:
    from .recheck import explain

    create_tables()
    explain(args.company, limit=args.limit)


def cmd_track(_args) -> None:
    from .alerts import AlertEngine
    from .notifiers import notify
    from .report import generate_report
    from .sources import build_tracking, build_wechat
    from .tracker import DailyTracker

    for p in _platforms():
        stats = DailyTracker(build_tracking() if p == "douyin" else build_wechat(), platform=p).run()
        engine = AlertEngine(platform=p)
        notify(engine.save(engine.evaluate(date.today(), stats.unavailable_ids)))
        generate_report(platform=p)


def cmd_report(args) -> None:
    import os

    from .report import generate_report

    for p in _platforms():
        path = generate_report(platform=p)
        print(f"跟踪表已更新：{path}")
        if args.open and hasattr(os, "startfile"):
            os.startfile(str(path))  # Windows：用默认程序（Excel/WPS）打开


def cmd_alerts(_args) -> None:
    from .alerts import AlertEngine
    from .notifiers import notify

    for p in _platforms():
        engine = AlertEngine(platform=p)
        notify(engine.save(engine.evaluate(date.today())))


def cmd_add_creator(args) -> None:
    create_tables()
    with session_scope() as s:
        for uid in args.sec_uids:
            uid = uid.rstrip("/").split("/user/")[-1].split("?")[0]
            c = s.get(DouyinFinanceCreator, uid)
            if c is None:
                s.add(DouyinFinanceCreator(sec_uid=uid, flagged_video_count=0, first_seen_at=datetime.now(),
                                           last_flagged_at=datetime.now(), enabled=True, added_by="manual"))
            else:
                c.enabled = True
            logger.info(f"已加入博主池：{uid}")


def cmd_douyin_login(_args) -> None:
    """只做抖音扫码登录 + 一次很小的测试搜索，登录状态会保存下来，之后每日任务不用再扫码。"""
    from .sources.base import SourceError
    from .sources.mediacrawler import MediaCrawlerSource

    print("即将打开 Chrome 和抖音登录二维码，请在 10 分钟内用抖音 App 扫码并确认登录。")
    try:
        result = MediaCrawlerSource(max_notes=5).search_recent(["A股"])
    except SourceError as e:
        print(f"登录或测试搜索失败：{e}")
        raise SystemExit(1)
    n = sum(len(v) for v in result.values())
    print(f"登录成功，测试搜索抓到 {n} 条视频。登录状态已保存，之后的每日任务会自动沿用。")


def cmd_selftest(args) -> None:
    from .selftest import run_selftest

    raise SystemExit(0 if run_selftest(include_tikhub=not args.no_tikhub) else 1)


def cmd_status(_args) -> None:
    create_tables()
    with session_scope() as s:
        print(f"知识库公司数：{s.scalar(select(func.count()).select_from(AShareCompany))}")
        for p, label in (("douyin", "抖音"), ("wechat", "视频号")):
            v = DouyinStockVideo
            total = s.scalar(select(func.count()).select_from(v).where(v.platform == p))
            flagged = s.scalar(select(func.count()).select_from(v).where(v.platform == p, v.is_company_analysis.is_(True)))
            active = s.scalar(select(func.count()).select_from(v).where(v.platform == p, v.tracking_status == "active"))
            creators = s.scalar(select(func.count()).select_from(DouyinFinanceCreator).where(
                DouyinFinanceCreator.platform == p, DouyinFinanceCreator.enabled.is_(True)))
            alerts7 = s.scalar(select(func.count()).select_from(StockVideoAlert).where(
                StockVideoAlert.platform == p, StockVideoAlert.alert_date >= date.today() - timedelta(days=6)))
            print(f"【{label}】已收录财经作品 {total}，上市公司分析 {flagged}，正在跟踪 {active}，博主池 {creators}，近 7 日预警 {alerts7}")


def cmd_schedule(args) -> None:
    hh, mm = (int(x) for x in args.at.split(":"))
    logger.info(f"定时模式：每天 {args.at} 执行一次（Ctrl+C 退出）。生产环境更推荐用 cron / systemd timer。")
    while True:
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        logger.info(f"下次执行：{target}")
        time.sleep((target - now).total_seconds())
        try:
            cmd_run(argparse.Namespace(skip_discovery=False, skip_tracking=False))
        except Exception as e:
            logger.exception(f"每日任务失败：{e}")


def main() -> None:
    get_settings()
    p = argparse.ArgumentParser(description="抖音 A 股上市公司分析视频监控")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db").set_defaults(func=cmd_init_db)

    kb = sub.add_parser("build-kb", help="构建/更新 A 股上市公司知识库")
    kb.add_argument("--skip-business", action="store_true", help="不拉取主营介绍（首次约需 30 分钟）")
    kb.add_argument("--refresh-business", action="store_true", help="重新拉取所有公司的主营介绍")
    kb.add_argument("--skip-llm", action="store_true", help="不用大模型扩充别名/产品/技术")
    kb.add_argument("--refresh-llm", action="store_true", help="对已扩充过的公司重新扩充")
    kb.add_argument("--enrich-limit", type=int, default=None, help="本次最多扩充多少家（试运行用）")
    kb.set_defaults(func=cmd_build_kb)

    run = sub.add_parser("run", help="执行一次每日任务")
    run.add_argument("--skip-discovery", action="store_true")
    run.add_argument("--skip-tracking", action="store_true")
    run.add_argument("--scheduled", action="store_true",
                     help="计划任务模式：必须在 STOCK_VIDEO_FINISH_BY（默认 06:50）前结束，启动太晚则跳过")
    run.set_defaults(func=cmd_run)

    sub.add_parser("track", help="只执行跟踪和预警").set_defaults(func=cmd_track)
    sub.add_parser("alerts", help="只重新计算今日预警").set_defaults(func=cmd_alerts)
    rp = sub.add_parser("report", help="更新每日跟踪表（Excel）")
    rp.add_argument("--open", action="store_true", help="生成后直接打开")
    rp.set_defaults(func=cmd_report)
    ac = sub.add_parser("add-creator", help="手动加入博主（sec_uid 或主页链接）")
    ac.add_argument("sec_uids", nargs="+")
    ac.set_defaults(func=cmd_add_creator)
    sub.add_parser("status", help="查看监控概况").set_defaults(func=cmd_status)
    rc = sub.add_parser("recheck", help="复核已标记作品，去掉作品文字中没有出现的公司（不调用大模型）")
    rc.add_argument("--dry-run", action="store_true", help="只显示会去掉哪些，不修改数据")
    rc.add_argument("--rejudge", action="store_true",
                    help="用大模型按新规则（只算主要分析对象）重新判断已标记作品；只用已保存的文字，不访问 TikHub")
    rc.add_argument("--workers", type=int, default=4)
    rc.set_defaults(func=cmd_recheck)
    why = sub.add_parser("why", help="查看某家公司为什么被挂到作品上（在哪段文字、靠哪个词匹配）")
    why.add_argument("company", help="公司简称或股票代码，例如 欣旺达 或 300207")
    why.add_argument("--limit", type=int, default=10)
    why.set_defaults(func=cmd_why)
    sub.add_parser("cleanup", help="清理临时文件、浏览器缓存、旧日志和旧数据").set_defaults(func=cmd_cleanup)
    sub.add_parser("douyin-login", help="抖音扫码登录（首次使用或登录失效时运行）").set_defaults(func=cmd_douyin_login)
    st = sub.add_parser("selftest", help="部署自检：数据库、大模型、TikHub、AKShare、内容解析")
    st.add_argument("--no-tikhub", action="store_true", help="跳过 TikHub 检查（避免产生费用）")
    st.set_defaults(func=cmd_selftest)
    sch = sub.add_parser("schedule", help="常驻进程，每天定时执行")
    sch.add_argument("--at", default="07:30", help="每天执行时间，默认 07:30（开盘前）")
    sch.set_defaults(func=cmd_schedule)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
