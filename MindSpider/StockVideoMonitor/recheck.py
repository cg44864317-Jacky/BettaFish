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
