# -*- coding: utf-8 -*-
"""
部署自检：逐项检查监控模块依赖的外部服务是否可用，并给出排查提示。

    python -m StockVideoMonitor.main selftest            # 全部检查（TikHub 搜索一次约 0.01 美元）
    python -m StockVideoMonitor.main selftest --no-tikhub
"""

from __future__ import annotations

import shutil
import time
from typing import Callable, List, Tuple

from .settings import MINDSPIDER_ROOT, get_settings

Result = Tuple[str, bool, str]


def _check(name: str, fn: Callable[[], str], hint: str) -> Result:
    t0 = time.time()
    try:
        detail = fn()
        return name, True, f"{detail}（{time.time() - t0:.1f}s）"
    except Exception as e:  # noqa: BLE001 —— 自检要把所有错误都展示出来
        return name, False, f"{type(e).__name__}: {str(e)[:300]}\n      → {hint}"


def check_database() -> str:
    from sqlalchemy import inspect

    from .db import create_tables, get_engine

    create_tables()
    tables = set(inspect(get_engine()).get_table_names())
    need = {"douyin_stock_video", "a_share_company", "douyin_stock_video_snapshot"}
    missing = need - tables
    if missing:
        raise RuntimeError(f"缺少数据表：{missing}")
    return f"已连接 {get_engine().url.render_as_string(hide_password=True)}，共 {len(tables)} 张表"


def check_llm() -> str:
    from .llm import OpenAICompatLLM

    llm = OpenAICompatLLM()
    data = llm.complete_json("只输出 JSON。", '请输出 {"ok": true, "company": "贵州茅台的股票代码"}，company 填 6 位代码。')
    return f"模型 {llm.model} 返回：{data}"


def check_tikhub() -> str:
    from .sources.tikhub import TikHubClient

    client = TikHubClient()
    recs = client.search_videos("A股", pages=1)
    if not recs:
        raise RuntimeError("接口可用但没有返回视频")
    sample = recs[0]
    return (f"搜到 {len(recs)} 条最近一天的作品；示例：{(sample.desc or '')[:30]}… "
            f"点赞 {sample.liked_count} 收藏 {sample.collected_count} 转发 {sample.share_count}")


def check_akshare() -> str:
    from .knowledge_base import fetch_company_list

    companies = fetch_company_list()
    return f"获取到 {len(companies)} 家 A 股公司"


def check_media() -> str:
    from .media import NoopFrameReader, NoopTranscriber, build_frame_reader, build_transcriber

    s = get_settings()
    parts = [f"ffmpeg：{'有' if shutil.which('ffmpeg') else '未找到（视频解析不可用）'}"]
    ocr = build_frame_reader(s)
    parts.append(f"画面文字识别：{s.STOCK_VIDEO_OCR_BACKEND}{'（未开启）' if isinstance(ocr, NoopFrameReader) else ''}")
    asr = build_transcriber(s)
    parts.append(f"语音识别：{s.STOCK_VIDEO_ASR_BACKEND}{'（未开启）' if isinstance(asr, NoopTranscriber) else ''}")
    return "；".join(parts)


def check_mediacrawler() -> str:
    main = MINDSPIDER_ROOT / "DeepSentimentCrawling" / "MediaCrawler" / "main.py"
    if not main.exists():
        raise RuntimeError("MediaCrawler 子模块不存在")
    return "MediaCrawler 已就绪（首次运行 run 时会弹出抖音扫码登录）"


def run_selftest(include_tikhub: bool = True) -> bool:
    s = get_settings()
    checks: List[Result] = [
        _check("数据库", check_database, "检查 .env 里的 DB_HOST / DB_USER / DB_PASSWORD / DB_NAME，以及 PostgreSQL 服务是否在运行"),
        _check("大模型", check_llm, "检查 .env 里的 MINDSPIDER_API_KEY / MINDSPIDER_BASE_URL / MINDSPIDER_MODEL_NAME（或 STOCK_VIDEO_LLM_*）"),
    ]
    if include_tikhub:
        if s.TIKHUB_API_KEY:
            checks.append(_check("TikHub", check_tikhub, "检查 TIKHUB_API_KEY 和账户余额；国内网络访问不了时可尝试修改 TIKHUB_BASE_URL"))
        else:
            checks.append(("TikHub", False, "未配置 TIKHUB_API_KEY\n      → 在 .env 中填写 TIKHUB_API_KEY"))
    checks += [
        _check("AKShare", check_akshare, "检查网络；AKShare 需要访问东方财富/同花顺等网站"),
        _check("内容解析", check_media, "检查 STOCK_VIDEO_OCR_BACKEND / STOCK_VIDEO_ASR_BACKEND 配置和相应依赖"),
        _check("MediaCrawler", check_mediacrawler, "在 BettaFish 目录运行 git submodule update --init --recursive"),
    ]
    print("\n========== 部署自检 ==========")
    for name, ok, detail in checks:
        print(f"[{'通过' if ok else '失败'}] {name}：{detail}")
    all_ok = all(ok for _, ok, _ in checks)
    print("==============================")
    print("全部通过，可以开始构建知识库并运行。" if all_ok else "有检查未通过，请按提示处理后重新运行自检。")
    return all_ok
