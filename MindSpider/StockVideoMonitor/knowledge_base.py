# -*- coding: utf-8 -*-
"""
A 股上市公司知识库：构建、扩充与文本匹配

构建步骤（建议每周运行一次 `python -m StockVideoMonitor.main build-kb`）：
1. AKShare 拉取全部 A 股代码与简称（stock_info_a_code_name）
2. 逐家拉取同花顺主营介绍（stock_zyjs_ths），得到主营业务和产品名称
3. 用大模型批量补充：常见叫法/外号、主要产品、核心技术、品牌
4. 人工修改过的公司（manual_locked=True）不会被自动覆盖别名、产品、技术和品牌

匹配：把所有词放进前缀树，一次扫描文本即可找出命中的公司，并记录是靠名称、产品、技术还是代码命中的。
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set, Tuple

from loguru import logger
from sqlalchemy import select

from .db import session_scope
from .llm import JSONLLM
from .models import AShareCompany, dumps, loads_list

# 太泛、容易误报的词，即使出现在产品/技术里也不用于匹配
GENERIC_TERMS = {
    "电池", "芯片", "汽车", "白酒", "银行", "保险", "证券", "软件", "服务", "平台", "系统", "设备", "材料",
    "医药", "药品", "手机", "电脑", "光伏", "储能", "钢材", "水泥", "地产", "物业", "物流", "能源", "电力",
    "数据", "云计算", "人工智能", "AI", "大模型", "机器人", "半导体", "新能源", "互联网", "游戏", "电商",
    "中国", "集团", "股份", "科技", "控股", "实业", "发展", "产品", "技术", "其他", "主营业务",
}
CODE_RE = re.compile(r"(?<!\d)((?:60|68|00|30)\d{4}|(?:8|4)\d{5}|920\d{3})(?!\d)")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "").replace(" ", "").replace("　", "").upper()


def exchange_of(code: str) -> str:
    if code.startswith(("60", "68")):
        return "SH"
    if code.startswith(("00", "30")):
        return "SZ"
    return "BJ"


def name_variants(name: str) -> List[str]:
    """简称的常见变体：去掉 ST / *ST 前缀、去空格、全角转半角。"""
    n = normalize(name)
    out = {n}
    stripped = re.sub(r"^\*?ST", "", n)
    if len(stripped) >= 2:
        out.add(stripped)
    if n.endswith(("A", "B")) and len(n) >= 3:  # 万科A → 万科
        out.add(n[:-1])
    return [x for x in out if x]


# ---------------------------------------------------------------------- 匹配器
@dataclass
class CompanyHit:
    stock_code: str
    name: str
    bases: Set[str] = field(default_factory=set)  # name / product / technology / brand / code
    terms: Set[str] = field(default_factory=set)


class CompanyMatcher:
    def __init__(self, companies: Iterable[AShareCompany]):
        self.names: Dict[str, str] = {}
        self._trie: dict = {}
        for c in companies:
            self.names[c.stock_code] = c.name
            for v in name_variants(c.name):
                self._add(v, c.stock_code, "name")
            for alias in loads_list(c.aliases):
                self._add(normalize(alias), c.stock_code, "name", min_len=2)
            for basis, values in (("product", c.products), ("technology", c.technologies), ("brand", c.brands)):
                for v in loads_list(values):
                    self._add(normalize(v), c.stock_code, basis, min_len=2 if c.manual_locked else 3)

    def _add(self, term: str, code: str, basis: str, min_len: int = 2) -> None:
        if len(term) < min_len or term in GENERIC_TERMS:
            return
        node = self._trie
        for ch in term:
            node = node.setdefault(ch, {})
        node.setdefault("$", set()).add((code, basis, term))

    def __len__(self) -> int:
        return len(self.names)

    def match(self, text: str) -> Dict[str, CompanyHit]:
        t = normalize(text)
        hits: Dict[str, CompanyHit] = {}
        for i in range(len(t)):
            node = self._trie
            j = i
            while j < len(t) and t[j] in node:
                node = node[t[j]]
                j += 1
                for code, basis, term in node.get("$", ()):
                    h = hits.setdefault(code, CompanyHit(code, self.names[code]))
                    h.bases.add(basis)
                    h.terms.add(term)
        for code in CODE_RE.findall(t):
            if code in self.names:
                h = hits.setdefault(code, CompanyHit(code, self.names[code]))
                h.bases.add("code")
                h.terms.add(code)
        return hits

    def resolve(self, code: Optional[str], name: Optional[str]) -> Optional[Tuple[str, str]]:
        """把大模型给出的代码/名称校正为知识库里真实存在的公司。"""
        if code:
            m = re.search(r"\d{6}", str(code))
            if m and m.group(0) in self.names:
                return m.group(0), self.names[m.group(0)]
        if name:
            hits = self.match(name)
            name_hits = [h for h in hits.values() if "name" in h.bases]
            if len(name_hits) == 1:
                return name_hits[0].stock_code, name_hits[0].name
        return None


def load_matcher() -> CompanyMatcher:
    with session_scope() as s:
        companies = list(s.scalars(select(AShareCompany)))
    if not companies:
        logger.warning("上市公司知识库为空，请先运行 build-kb")
    return CompanyMatcher(companies)


# ---------------------------------------------------------------------- 构建
def _col(df, *candidates: str) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def fetch_company_list() -> List[Tuple[str, str]]:
    import akshare as ak

    df = ak.stock_info_a_code_name()
    code_col, name_col = _col(df, "code", "证券代码", "代码"), _col(df, "name", "证券简称", "名称")
    return [(str(r[code_col]).zfill(6), str(r[name_col]).strip()) for _, r in df.iterrows()]


def fetch_main_business(code: str) -> Tuple[Optional[str], List[str]]:
    import akshare as ak

    df = ak.stock_zyjs_ths(symbol=code)
    if df is None or df.empty:
        return None, []
    row = df.iloc[0]
    business = row.get("主营业务") or None
    products: List[str] = []
    for col in ("产品名称", "产品类型"):
        val = row.get(col)
        if isinstance(val, str):
            products.extend(p.strip() for p in re.split(r"[、,，;；/]", val) if p.strip())
    return business, list(dict.fromkeys(products))


def sync_company_list(fetch_business: bool = True, sleep: float = 0.3, only_missing: bool = True) -> int:
    """同步公司列表与主营介绍，返回新增/更新的公司数。"""
    companies = fetch_company_list()
    logger.info(f"[知识库] AKShare 返回 {len(companies)} 家 A 股公司")
    now, changed = datetime.now(), 0
    with session_scope() as s:
        existing = {c.stock_code: c for c in s.scalars(select(AShareCompany))}
        for code, name in companies:
            c = existing.get(code)
            if c is None:
                c = AShareCompany(stock_code=code, name=name, exchange=exchange_of(code), manual_locked=False)
                s.add(c)
                existing[code] = c
                changed += 1
            elif c.name != name:  # 改名：旧名留作别名
                aliases = loads_list(c.aliases)
                if c.name not in aliases:
                    aliases.append(c.name)
                c.aliases, c.name = dumps(aliases), name
                changed += 1
            c.updated_at = now
    if not fetch_business:
        return changed
    with session_scope() as s:
        todo = [c for c in s.scalars(select(AShareCompany)) if not (only_missing and c.main_business)]
    logger.info(f"[知识库] 拉取 {len(todo)} 家公司的主营介绍（约 {len(todo) * sleep / 60:.0f} 分钟）")
    for i, c in enumerate(todo, 1):
        try:
            business, products = fetch_main_business(c.stock_code)
        except Exception as e:
            logger.debug(f"[知识库] {c.stock_code} 主营介绍获取失败：{e}")
            continue
        with session_scope() as s:
            row = s.get(AShareCompany, c.stock_code)
            row.main_business = business
            if not row.manual_locked and products:
                row.products = dumps(list(dict.fromkeys(loads_list(row.products) + products)))
        if i % 200 == 0:
            logger.info(f"[知识库] 主营介绍进度 {i}/{len(todo)}")
        time.sleep(sleep)
    return changed


ENRICH_SYSTEM = """你是 A 股上市公司研究员。给定一批公司的代码、简称和主营业务，为每家公司补充以下信息，用于在短视频文字中识别该公司：
- aliases：投资者常用的叫法、外号、旧称（如 宁德时代→宁王；贵州茅台→茅台）。
- products：有辨识度的主要产品或业务名称（如 飞天茅台、麒麟电池、元宝）。
- technologies：有辨识度的核心技术、平台或工艺名称（如 CTP、刀片电池）。
- brands：旗下知名品牌。
要求：只写能指向这家公司的具体名称，不写「电池」「芯片」「白酒」这类行业通用词；不确定就留空；每类最多 8 个。
只输出 JSON：{"companies":[{"code":"600519","aliases":[],"products":[],"technologies":[],"brands":[]}]}"""


def enrich_with_llm(llm: JSONLLM, batch_size: int = 15, limit: Optional[int] = None, refresh: bool = False) -> int:
    with session_scope() as s:
        q = select(AShareCompany).where(AShareCompany.manual_locked.is_(False))
        if not refresh:
            q = q.where(AShareCompany.enriched_at.is_(None))
        todo = list(s.scalars(q.order_by(AShareCompany.stock_code)))
    if limit:
        todo = todo[:limit]
    done = 0
    for i in range(0, len(todo), batch_size):
        batch = todo[i : i + batch_size]
        lines = [
            f"{c.stock_code} {c.name}：{(c.main_business or '')[:120]}；已知产品：{'、'.join(loads_list(c.products)[:10])}"
            for c in batch
        ]
        try:
            data = llm.complete_json(ENRICH_SYSTEM, "\n".join(lines))
        except Exception as e:
            logger.warning(f"[知识库] 大模型扩充失败（第 {i // batch_size + 1} 批）：{e}")
            continue
        items = {str(x.get("code", "")).zfill(6): x for x in (data or {}).get("companies", []) if isinstance(x, dict)}
        with session_scope() as s:
            for c in batch:
                x = items.get(c.stock_code)
                if not x:
                    continue
                row = s.get(AShareCompany, c.stock_code)
                for attr in ("aliases", "products", "technologies", "brands"):
                    merged = loads_list(getattr(row, attr)) + [str(v).strip() for v in x.get(attr, []) if str(v).strip()]
                    setattr(row, attr, dumps(list(dict.fromkeys(merged))))
                row.enriched_at = datetime.now()
                done += 1
        logger.info(f"[知识库] 大模型扩充进度 {min(i + batch_size, len(todo))}/{len(todo)}")
    return done


def fetch_hot_stock_names(top_n: int) -> List[str]:
    """东方财富人气榜前 N 名的股票简称，用作当天的动态搜索词。"""
    if top_n <= 0:
        return []
    try:
        import akshare as ak

        df = ak.stock_hot_rank_em()
        col = _col(df, "股票名称", "名称")
        return [str(x).strip() for x in df[col].head(top_n)] if col else []
    except Exception as e:
        logger.warning(f"[发现] 人气榜获取失败，跳过动态关键词：{e}")
        return []
