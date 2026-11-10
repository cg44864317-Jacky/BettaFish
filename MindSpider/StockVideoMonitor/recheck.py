# -*- coding: utf-8 -*-
"""
复核已标记作品里的公司：只保留在作品文字（标题、文案、话题、语音文字、画面文字）中真实出现的公司。

用于清理改进识别规则之前留下的误标（例如讲宁德时代的视频被同时挂上欣旺达）。
只用已保存的文字和知识库做匹配，不调用大模型，不产生费用。
一条作品的公司全部被去掉时，取消它的「上市公司分析」标记并停止跟踪。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Tuple

from loguru import logger
from sqlalchemy import delete, select

from .db import session_scope
from .knowledge_base import CompanyMatcher, load_matcher, loads_list
from .models import DouyinStockVideo, DouyinStockVideoCompany


@dataclass
class RecheckStats:
    videos: int = 0
    removed_links: int = 0
    unflagged: int = 0
    examples: List[Tuple[str, str, str]] = field(default_factory=list)  # (作品标题, 去掉的公司, 平台)


def video_text(v: DouyinStockVideo) -> str:
    tags = " ".join("#" + t for t in loads_list(v.hashtags))
    return "\n".join(x for x in (v.title, v.desc, tags, v.asr_text, v.ocr_text) if x)


def recheck(matcher: CompanyMatcher = None, dry_run: bool = False) -> RecheckStats:
    matcher = matcher or load_matcher()
    stats = RecheckStats()
    with session_scope() as s:
        videos = list(s.scalars(select(DouyinStockVideo).where(DouyinStockVideo.is_company_analysis.is_(True))))
        for v in videos:
            stats.videos += 1
            hits = matcher.match(video_text(v))
            links = list(s.scalars(select(DouyinStockVideoCompany).where(DouyinStockVideoCompany.aweme_id == v.aweme_id)))
            bad = [m for m in links if not matcher.grounded(m.stock_code, hits)]
            if not bad:
                continue
            for m in bad:
                stats.removed_links += 1
                if len(stats.examples) < 15:
                    stats.examples.append(((v.title or v.desc or "")[:30], f"{m.stock_name}({m.stock_code})", v.platform))
            if not dry_run:
                s.execute(delete(DouyinStockVideoCompany).where(
                    DouyinStockVideoCompany.id.in_([m.id for m in bad])))
            if len(bad) == len(links):
                stats.unflagged += 1
                if not dry_run:
                    v.is_company_analysis = False
                    v.is_tracked = False
                    v.tracking_status = "none"
        if dry_run:
            s.rollback()
    verb = "将" if dry_run else "已"
    logger.info(f"[复核] 检查 {stats.videos} 条已标记作品：{verb}去掉 {stats.removed_links} 个作品中未出现的公司，"
                f"{verb}取消 {stats.unflagged} 条作品的标记（公司全部不成立）")
    for title, comp, platform in stats.examples:
        logger.info(f"[复核]   {'视频号' if platform == 'wechat' else '抖音'}「{title}」去掉 {comp}")
    return stats


def rejudge(workers: int = 4, dry_run: bool = False) -> RecheckStats:
    """用新的识别规则（只算「主要分析对象」）对已标记作品重新判断一次。
    只用已保存的文字（标题、文案、话题、语音文字、画面文字）调用大模型，不访问 TikHub、不下载视频。"""
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from .classifier import TwoStageClassifier
    from .llm import OpenAICompatLLM
    from .settings import get_settings

    s_ = get_settings()
    clf = TwoStageClassifier(load_matcher(), OpenAICompatLLM(s_), s_.STOCK_VIDEO_PREFILTER_BATCH, s_.STOCK_VIDEO_MIN_CONFIDENCE)
    stats = RecheckStats()
    lock = threading.Lock()
    with session_scope() as s:
        items = [(v.aweme_id, "\n".join(x for x in (v.title, v.desc, " ".join("#" + t for t in loads_list(v.hashtags))) if x),
                  v.asr_text or "", v.ocr_text or "", v.platform, (v.title or v.desc or "")[:30])
                 for v in s.scalars(select(DouyinStockVideo).where(DouyinStockVideo.is_company_analysis.is_(True)))]
    total = len(items)
    logger.info(f"[重判] 共 {total} 条已标记作品，{workers} 条同时处理{'（试运行，不修改数据）' if dry_run else ''}")

    def work(item):
        aid, text, asr, ocr, platform, title = item
        try:
            r = clf.judge(text, asr, ocr)
        except Exception as e:  # 单条失败保持原样
            logger.warning(f"[重判] {aid} 失败，保持原样：{e}")
            return
        with lock, session_scope() as s:
            stats.videos += 1
            links = list(s.scalars(select(DouyinStockVideoCompany).where(DouyinStockVideoCompany.aweme_id == aid)))
            keep = {m.stock_code for m in r.companies} if r.is_company_analysis else set()
            bad = [m for m in links if m.stock_code not in keep]
            for m in bad:
                stats.removed_links += 1
                if len(stats.examples) < 30:
                    stats.examples.append((title, f"{m.stock_name}({m.stock_code})", platform))
            if dry_run:
                return
            for m in bad:
                s.delete(m)
            v = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == aid))
            if not keep or len(bad) == len(links):
                stats.unflagged += 1
                v.is_company_analysis, v.is_tracked, v.tracking_status = False, False, "none"
            v.analysis_raw = json.dumps(r.raw, ensure_ascii=False)[:20000] if r.raw else v.analysis_raw
            if stats.videos % 50 == 0:
                logger.info(f"[重判] 进度 {stats.videos}/{total}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(work, items))
    verb = "将" if dry_run else "已"
    logger.info(f"[重判] 完成 {stats.videos}/{total} 条：{verb}去掉 {stats.removed_links} 个非主要分析对象的公司，"
                f"{verb}取消 {stats.unflagged} 条作品的标记")
    for title, comp, platform in stats.examples:
        logger.info(f"[重判]   {'视频号' if platform == 'wechat' else '抖音'}「{title}」去掉 {comp}")
    return stats


FIELD_CN = (("title", "标题"), ("desc", "文案"), ("hashtags", "话题"), ("asr_text", "语音文字"), ("ocr_text", "画面文字"))


def explain(company: str, matcher: CompanyMatcher = None, limit: int = 10) -> int:
    """显示某家公司被挂到哪些作品上，以及它是在哪段文字里、靠哪个词匹配上的。"""
    from .knowledge_base import normalize

    matcher = matcher or load_matcher()
    with session_scope() as s:
        q = select(DouyinStockVideoCompany).where(
            (DouyinStockVideoCompany.stock_name == company) | (DouyinStockVideoCompany.stock_code == company))
        links = list(s.scalars(q.order_by(DouyinStockVideoCompany.id.desc()).limit(limit)))
        print(f"「{company}」挂在 {len(links)} 条作品上（最多显示 {limit} 条）")
        for m in links:
            v = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == m.aweme_id))
            if not v:
                continue
            print(f"\n■ {'视频号' if v.platform == 'wechat' else '抖音'}「{(v.title or v.desc or '')[:40]}」 {v.aweme_url or ''}")
            print(f"  大模型给出的依据：{m.basis}；原文证据：{(m.evidence or '')[:60]}")
            found = False
            for attr, label in FIELD_CN:
                raw = getattr(v, attr) or ""
                text = " ".join(loads_list(raw)) if attr == "hashtags" else raw
                hit = matcher.match(text).get(m.stock_code)
                if not hit:
                    continue
                found = True
                t = normalize(text)
                for term in sorted(hit.terms):
                    i = t.find(term)
                    ctx = t[max(0, i - 20): i + len(term) + 20].replace("\n", " ") if i >= 0 else ""
                    print(f"  {label}里匹配到「{term}」（{len(matcher.owners.get(term, ()))} 家公司共有此词）：…{ctx}…")
            if not found:
                print("  作品文字里找不到这家公司（运行 recheck 会去掉）")
    return len(links)
