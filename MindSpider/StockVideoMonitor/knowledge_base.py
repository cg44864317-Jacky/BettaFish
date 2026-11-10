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
    "龙头", "龙头股", "牛股", "妖股", "白马", "蓝筹", "概念股", "板块",
}
# 公司简称本身就是日常用语，只作为弱候选（大模型扩充时还会补充 ambiguous_terms）
COMMON_WORD_NAMES = {"值得买", "农产品", "中国长城", "长城", "天马", "宝安", "中集", "特发", "华强", "天健", "沙河", "赛格"}
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
    strong: bool = False  # 是否有「强」命中：股票代码，或不会与日常用语混淆的名称/外号


class CompanyMatcher:
    """
    强词：股票代码、≥3 个字且只属于一家公司、且未被标为日常用语的名称/外号。
    弱词：其余名称/外号，以及产品、技术、品牌。弱词只作为候选交给大模型判断，
          不会单独把视频判为股票相关（避免「值得买」「农产品」「高速公路」这类误报）。
    产品/技术/品牌只取大模型扩充或人工维护过的公司，AKShare 的业务板块名称不参与匹配。
    """

    def __init__(self, companies: Iterable[AShareCompany]):
        self.names: Dict[str, str] = {}
        entries: List[Tuple[str, str, str]] = []  # (term, code, basis)
        ambiguous: Dict[str, Set[str]] = {}
        for c in companies:
            self.names[c.stock_code] = c.name
            # 大模型偶尔把公司自己的正式简称标为日常用语（如 比亚迪、盐田港），≥3 字的正式简称不采纳；内置的日常用语名单仍然生效
            own_official = {v for v in name_variants(c.name) if len(v) >= 3}
            amb = ({normalize(x) for x in loads_list(getattr(c, "ambiguous_terms", None))} - own_official) | COMMON_WORD_NAMES
            ambiguous[c.stock_code] = amb
            for v in name_variants(c.name):
                entries.append((v, c.stock_code, "name"))
            for alias in loads_list(c.aliases):
                entries.append((normalize(alias), c.stock_code, "name"))
            if c.enriched_at or c.manual_locked:
                for basis, values in (("product", c.products), ("technology", c.technologies), ("brand", c.brands)):
                    for v in loads_list(values):
                        entries.append((normalize(v), c.stock_code, basis))
        owners: Dict[str, Set[str]] = {}
        for term, code, _ in entries:
            owners.setdefault(term, set()).add(code)
        self.owners = owners
        self._trie: dict = {}
        for term, code, basis in entries:
            if len(term) < 2 or term in GENERIC_TERMS:
                continue
            strong = (basis == "name" and len(term) >= 3 and len(owners[term]) == 1
                      and term not in ambiguous.get(code, set()))
            node = self._trie
            for ch in term:
                node = node.setdefault(ch, {})
            node.setdefault("$", set()).add((code, basis, term, strong))

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
                for code, basis, term, strong in node.get("$", ()):
                    h = hits.setdefault(code, CompanyHit(code, self.names[code]))
                    h.bases.add(basis)
                    h.terms.add(term)
                    h.strong = h.strong or strong
        for code in CODE_RE.findall(t):
            if code in self.names:
                h = hits.setdefault(code, CompanyHit(code, self.names[code]))
                h.bases.add("code")
                h.terms.add(code)
                h.strong = True
        return hits

    def grounded(self, code: str, hits: Dict[str, CompanyHit], max_shared: int = 2) -> bool:
        """这家公司是否真的出现在作品文字里：名称/简称/外号/代码命中，或命中只属于它（最多 max_shared 家共有）的产品/技术/品牌。
        只靠「动力电池」这类许多公司共有的词命中的，不算出现。"""
        h = hits.get(code)
        if not h:
            return False
        if h.strong or "code" in h.bases or "name" in h.bases:
            return True
        return any(len(self.owners.get(t, ())) <= max_shared for t in h.terms)

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
            # 业务板块名称（如「高速公路」「房产销售」）太泛，只作为大模型扩充的背景材料，不进入匹配用的 products
            row.main_business = business + (f"（产品与业务：{'、'.join(products)}）" if products else "") if business else (
                f"产品与业务：{'、'.join(products)}" if products else None)
        if i % 200 == 0:
            logger.info(f"[知识库] 主营介绍进度 {i}/{len(todo)}")
        time.sleep(sleep)
    return changed


ENRICH_SYSTEM = """你是 A 股上市公司研究员。给定一批公司的代码、简称和主营业务，为每家公司补充以下信息，用于在短视频文字中识别该公司：
- aliases：投资者常用的叫法、外号、旧称（如 宁德时代→宁王；贵州茅台→茅台）。不要写纯地名。
- products：有辨识度的具体产品、型号或系列名称（如 飞天茅台、麒麟电池、萤石、泊寓）。
  不要写业务板块或品类名称，如「商品住宅」「高速公路」「房产销售」「显示屏」「药品」「租赁业务」。
- technologies：有辨识度的核心技术、平台或工艺名称（如 CTP、刀片电池、DM-i）。
- brands：旗下知名品牌。
- ambiguous_terms：该公司的简称或上面列出的外号中，属于日常用语、地名或常见词、容易在与股票无关的内容里出现的词
  （如 值得买、农产品、长城、宝安）。没有就留空。
要求：只写能指向这家公司的具体名称，不写「电池」「芯片」「白酒」这类行业通用词；不确定就留空；每类最多 8 个。
只输出 JSON：{"companies":[{"code":"600519","aliases":[],"products":[],"technologies":[],"brands":[],"ambiguous_terms":[]}]}"""


# 提示词示例里出现的词，只允许留在对应的公司上（大模型偶尔会把示例错配给别的公司，如把「宁王」写给贵州茅台）
EXAMPLE_TERM_OWNERS = {
    "宁王": "300750", "CATL": "300750", "麒麟电池": "300750", "神行电池": "300750", "CTP": "300750",
    "茅台": "600519", "飞天茅台": "600519", "刀片电池": "002594", "DM-I": "002594", "萤石": "002415",
    "泊寓": "000002", "值得买": "300785", "什么值得买": "300785", "农产品": "000061",
}


def cleanup_knowledge_base() -> int:
    """
    扩充后的清理，返回改动的公司数：
    - 提示词示例词只保留在正确的公司上
    - 一家公司的正式简称不能出现在其他公司的外号/产品/技术/品牌里（如「平安银行」不应挂在中国平安名下）
    人工维护（manual_locked）的公司不改动。
    """
    with session_scope() as s:
        companies = list(s.scalars(select(AShareCompany)))
        official: Dict[str, str] = {}
        for c in companies:
            for v in name_variants(c.name):
                if len(v) >= 2:
                    official.setdefault(v, c.stock_code)
        changed = 0
        for c in companies:
            if c.manual_locked:
                continue
            dirty = False
            for attr in ("aliases", "products", "technologies", "brands"):
                values = loads_list(getattr(c, attr))
                kept = []
                for v in values:
                    n = normalize(str(v))
                    owner = EXAMPLE_TERM_OWNERS.get(n) or official.get(n)
                    if owner and owner != c.stock_code:
                        continue
                    kept.append(v)
                if kept != values:
                    setattr(c, attr, dumps(kept))
                    dirty = True
            changed += dirty
    logger.info(f"[知识库] 清理完成，修正 {changed} 家公司的外号/产品/品牌")
    return changed


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
            f"{c.stock_code} {c.name}：{(c.main_business or '')[:300]}"
            + ("" if "产品与业务" in (c.main_business or "") or not loads_list(c.products)
               else f"（产品与业务：{'、'.join(loads_list(c.products)[:12])}）")
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
                clean = lambda key: list(dict.fromkeys(str(v).strip() for v in (x.get(key) or []) if str(v).strip()))  # noqa: E731
                # 外号与已有别名（改名留下的旧称）合并；产品/技术/品牌/歧义词以本次大模型输出为准
                row.aliases = dumps(list(dict.fromkeys(loads_list(row.aliases) + clean("aliases"))))
                for attr in ("products", "technologies", "brands", "ambiguous_terms"):
                    setattr(row, attr, dumps([v for v in clean(attr) if v not in GENERIC_TERMS]))
                row.enriched_at = datetime.now()
                done += 1
        logger.info(f"[知识库] 大模型扩充进度 {min(i + batch_size, len(todo))}/{len(todo)}")
    return done


HOT_SOURCE_USED: Optional[str] = None  # 最近一次人气股名单来自哪里（自检显示用）


def _hot_from_eastmoney(top_n: int) -> List[str]:
    import akshare as ak

    df = ak.stock_hot_rank_em()
    col = _col(df, "股票名称", "名称")
    return [str(x).strip() for x in df[col].head(top_n)] if col else []


def _hot_from_xueqiu(top_n: int) -> List[str]:
    import akshare as ak

    df = ak.stock_hot_follow_xq(symbol="最热门")
    col = _col(df, "股票简称", "名称")
    code_col = _col(df, "股票代码", "代码")
    if not col:
        return []
    if code_col:  # 只要 A 股（SH/SZ/BJ 开头）
        df = df[df[code_col].astype(str).str.upper().str.match(r"^(SH|SZ|BJ)")]
    return [str(x).strip() for x in df[col].head(top_n)]


def _hot_from_own_data(top_n: int, days: int = 7) -> List[str]:
    """外部人气榜都取不到时（如海外服务器访问东方财富被拒），用最近几天被标记最多的公司。"""
    from datetime import datetime, timedelta

    from sqlalchemy import func, select

    from .db import session_scope
    from .models import DouyinStockVideoCompany

    since = int((datetime.now() - timedelta(days=days)).timestamp())
    with session_scope() as s:
        rows = s.execute(
            select(DouyinStockVideoCompany.stock_name, func.count().label("n"))
            .where(DouyinStockVideoCompany.video_create_time >= since, DouyinStockVideoCompany.stock_name.isnot(None))
            .group_by(DouyinStockVideoCompany.stock_name)
            .order_by(func.count().desc())
            .limit(top_n)
        ).all()
    return [r[0] for r in rows]


def fetch_hot_stock_names(top_n: int) -> List[str]:
    """当天的人气股简称，用作动态搜索词：东方财富人气榜 → 雪球关注榜 → 本库近 7 天被标记最多的公司。"""
    global HOT_SOURCE_USED
    HOT_SOURCE_USED = None
    if top_n <= 0:
        return []
    sources = [("东方财富人气榜", _hot_from_eastmoney), ("东方财富人气榜", _hot_from_eastmoney),
               ("雪球关注榜", _hot_from_xueqiu), ("近 7 天被标记最多的公司", _hot_from_own_data)]
    for label, fn in sources:
        try:
            names = [n for n in fn(top_n) if n]
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[发现] {label}获取失败：{str(e)[:150]}")
            continue
        if names:
            HOT_SOURCE_USED = label
            logger.info(f"[发现] 动态关键词来自{label}：{len(names)} 个")
            return names
    logger.warning("[发现] 人气股名单都取不到，本次跳过动态关键词")
    return []
