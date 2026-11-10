# -*- coding: utf-8 -*-
"""
两级识别：
1. 粗筛：只看标题、文案、话题标签，批量判断是否与股票/上市公司投资相关（便宜、快）
2. 精判：对粗筛通过的视频，结合语音文字、画面文字和知识库候选公司，
   判断是否在「具体分析 A 股上市公司」，并给出涉及的公司、识别依据、观点和原文证据
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from .knowledge_base import CompanyHit, CompanyMatcher
from .llm import JSONLLM

FINANCE_HINTS = (
    "股", "A股", "涨停", "跌停", "板块", "龙头", "主力", "资金", "财报", "业绩", "估值", "市盈率", "利好", "利空",
    "仓位", "抄底", "大盘", "上证", "深成指", "创业板", "科创板", "北交所", "券商", "研报", "分红", "回购",
)

PREFILTER_SYSTEM = """你在为证券研究团队筛选抖音短视频。对每条视频（只给出标题、文案、话题标签），判断它是否与「股票投资 / 上市公司 / A股市场」相关。
相关：个股或板块分析、公司业绩/产品/技术解读、市场行情点评、资金流向、财经新闻对股票的影响。
不相关：理财课程或开户广告（没有实际分析内容）、只是字面含「股」但与股市无关的内容（如股骨、股份制改革科普）、生活娱乐。
拿不准时判为相关（后续还有更细的判断）。
只输出 JSON：{"results":[{"i":0,"related":true}]}"""

JUDGE_SYSTEM = """你是 A 股研究员，判断一条抖音作品（视频或图文）是否在「具体分析某家 A 股上市公司」。图文作品没有语音，「画面文字」是图片中识别出的文字。
判为 true 的条件：视频对至少一家 A 股上市公司有实质性讨论——它的业务、产品、技术、业绩、估值、股价走势、事件影响、投资逻辑等，
公司可以通过名称、简称/外号、股票代码、产品或技术被提及（例如只说「麒麟电池」也可能是在分析宁德时代）。
判为 false：只谈大盘或板块而不落到具体公司；只是一串股票名单、没有任何分析；纯引流广告；公司只是顺带一提。
注意：
- 只输出 A 股（沪深北交易所）上市公司；港股、美股、未上市公司（如华为）不算。
- 只输出在【标题/文案/话题】【语音文字】【画面文字】中实际出现的公司（出现名称、简称/外号、股票代码，或该公司特有的产品/技术）。
  不要凭行业知识补充没有出现的同行、竞争对手、上下游或供应链公司；例如视频只讲宁德时代，就不要输出欣旺达、亿纬锂能。
- basis 取值：name（名称/简称/外号）、code（股票代码）、product（产品）、technology（技术）。
- stance 取值：bullish（看多）、bearish（看空）、neutral（中性/仅介绍）。
- evidence 摘录原文中的关键句（不超过 60 字）。
- 「知识库候选」是关键词匹配结果，可能有误，请结合上下文判断；evidence 必须摘自上面的原文。
只输出 JSON：
{"is_company_analysis":true,"confidence":0.0,"summary":"一句话概括视频内容","companies":[{"code":"600519","name":"贵州茅台","basis":"name","stance":"bullish","evidence":"..."}]}"""


@dataclass
class CompanyMention:
    stock_code: str
    stock_name: str
    basis: str
    stance: str
    evidence: str
    confidence: float


@dataclass
class JudgeResult:
    is_company_analysis: bool
    confidence: float
    summary: str
    companies: List[CompanyMention] = field(default_factory=list)
    raw: Optional[dict] = None


def rule_finance_related(text: str, hits: Dict[str, CompanyHit]) -> bool:
    if any(h.strong for h in hits.values()):
        return True
    return any(k in text for k in FINANCE_HINTS)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + "…"


class TwoStageClassifier:
    def __init__(self, matcher: CompanyMatcher, llm: Optional[JSONLLM], batch_size: int = 20, min_confidence: float = 0.6):
        self.matcher = matcher
        self.llm = llm
        self.batch_size = batch_size
        self.min_confidence = min_confidence

    # -------------------------------------------------------------- 粗筛
    def prefilter(self, texts: List[str]) -> List[bool]:
        hits = [self.matcher.match(t) for t in texts]
        if self.llm is None:
            return [rule_finance_related(t, h) for t, h in zip(texts, hits)]
        out: List[bool] = []
        for start in range(0, len(texts), self.batch_size):
            chunk = texts[start : start + self.batch_size]
            lines = [f"[{i}] {_clip(t, 220)}" for i, t in enumerate(chunk)]
            try:
                data = self.llm.complete_json(PREFILTER_SYSTEM, "\n".join(lines))
                verdict = {int(r["i"]): bool(r.get("related")) for r in data.get("results", []) if "i" in r}
            except Exception as e:
                logger.warning(f"[粗筛] 大模型失败，改用规则判断：{e}")
                verdict = {}
            for i, t in enumerate(chunk):
                # 大模型漏判的条目按规则补；知识库命中公司名/代码的一律放行，避免漏掉
                h = hits[start + i]
                strong = any(x.strong for x in h.values())
                out.append(strong or verdict.get(i, rule_finance_related(t, h)))
        return out

    # -------------------------------------------------------------- 精判
    def judge(self, text: str, asr_text: str = "", ocr_text: str = "") -> JudgeResult:
        full = "\n".join(x for x in (text, asr_text, ocr_text) if x)
        candidates = self.matcher.match(full)
        cand_lines = [
            f"{h.stock_code} {h.name}（命中：{'、'.join(sorted(h.terms))[:60]}；依据：{'/'.join(sorted(h.bases))}"
            f"{'' if h.strong else '；弱匹配，可能是日常用语'}）"
            for h in sorted(candidates.values(), key=lambda x: (not x.strong, -len(x.terms)))[:30]
        ]
        if self.llm is None:
            return self._rule_judge(candidates)
        user = (
            f"【标题/文案/话题】\n{_clip(text, 800)}\n\n"
            f"【语音文字】\n{_clip(asr_text, 3000) or '（无）'}\n\n"
            f"【画面文字】\n{_clip(ocr_text, 1500) or '（无）'}\n\n"
            f"【知识库候选】\n{chr(10).join(cand_lines) or '（无）'}"
        )
        data = self.llm.complete_json(JUDGE_SYSTEM, user)
        return self._parse_judge(data, candidates)

    def _parse_judge(self, data: dict, candidates: Dict[str, CompanyHit]) -> JudgeResult:
        conf = float(data.get("confidence") or 0)
        mentions: Dict[str, CompanyMention] = {}
        for c in data.get("companies") or []:
            if not isinstance(c, dict):
                continue
            resolved = self.matcher.resolve(c.get("code"), c.get("name"))
            if not resolved:
                logger.debug(f"[精判] 丢弃无法在知识库中找到的公司：{c}")
                continue
            code, name = resolved
            if not self.matcher.grounded(code, candidates):  # 作品文字里根本没有这家公司，多半是大模型按行业联想补充的
                logger.info(f"[精判] 丢弃作品中未出现的公司：{code} {name}（大模型给出的依据：{str(c.get('evidence'))[:40]}）")
                continue
            basis = c.get("basis") if c.get("basis") in ("name", "code", "product", "technology") else None
            if basis is None:
                bases = candidates.get(code).bases if code in candidates else set()
                basis = next((b for b in ("name", "code", "product", "technology") if b in bases), "name")
            stance = c.get("stance") if c.get("stance") in ("bullish", "bearish", "neutral") else "neutral"
            mentions.setdefault(code, CompanyMention(code, name, basis, stance, str(c.get("evidence") or "")[:300], conf))
        flagged = bool(data.get("is_company_analysis")) and bool(mentions) and conf >= self.min_confidence
        return JudgeResult(flagged, conf, str(data.get("summary") or "")[:500], list(mentions.values()), data)

    @staticmethod
    def _rule_judge(candidates: Dict[str, CompanyHit]) -> JudgeResult:
        """没有大模型时的退化逻辑：命中公司名称或代码即视为分析视频（仅用于测试/应急）。"""
        strong = [h for h in candidates.values() if h.strong]
        mentions = [
            CompanyMention(h.stock_code, h.name, "code" if "code" in h.bases else "name", "neutral", "、".join(sorted(h.terms)), 0.5)
            for h in strong
        ]
        return JudgeResult(bool(mentions), 0.5 if mentions else 0.0, "规则判断（未配置大模型）", mentions)
