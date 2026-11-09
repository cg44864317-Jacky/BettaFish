# -*- coding: utf-8 -*-
"""抖音 A 股上市公司分析视频监控模块测试（SQLite + 模拟数据源，不访问网络）。"""

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "MindSpider"))

from StockVideoMonitor import db as mdb  # noqa: E402
from StockVideoMonitor.alerts import AlertEngine, daily_deltas  # noqa: E402
from StockVideoMonitor.classifier import TwoStageClassifier  # noqa: E402
from StockVideoMonitor.discovery import DailyDiscovery  # noqa: E402
from StockVideoMonitor.knowledge_base import CompanyMatcher, name_variants  # noqa: E402
from StockVideoMonitor.llm import extract_json  # noqa: E402
from StockVideoMonitor import media as media_mod  # noqa: E402
from StockVideoMonitor.media import MediaAnalyzer, NoopFrameReader, NoopTranscriber  # noqa: E402
from StockVideoMonitor.models import (  # noqa: E402
    AShareCompany, DouyinFinanceCreator, DouyinStockVideo, DouyinStockVideoCompany, DouyinStockVideoSnapshot,
    StockVideoAlert, dumps,
)
from StockVideoMonitor.settings import MonitorSettings, override_settings  # noqa: E402
from StockVideoMonitor.sources.base import DiscoverySource, TrackingSource, VideoRecord  # noqa: E402
from StockVideoMonitor.sources.mediacrawler import _set_config_lines  # noqa: E402
from StockVideoMonitor.sources import tikhub as th  # noqa: E402
from StockVideoMonitor.tracker import DailyTracker  # noqa: E402


# ---------------------------------------------------------------------- 公共夹具
def make_settings(tmp_path, **kw) -> MonitorSettings:
    base = dict(
        DATABASE_URL=f"sqlite:///{tmp_path / 'monitor.db'}", TIKHUB_API_KEY="test-token",
        STOCK_VIDEO_SEARCH_KEYWORDS=["股票", "A股"], STOCK_VIDEO_HOT_STOCK_KEYWORDS=0,
        STOCK_VIDEO_MEDIA_DIR=str(tmp_path / "media"), TIKHUB_MAX_RETRIES=1,
    )
    base.update(kw)
    return MonitorSettings(_env_file=None, **base)


@pytest.fixture
def settings(tmp_path):
    s = make_settings(tmp_path)
    override_settings(s)
    engine = create_engine(s.DATABASE_URL, future=True)
    mdb.reset_engine(engine)
    mdb.create_tables(engine)
    yield s
    mdb.reset_engine(None)
    engine.dispose()


COMPANIES = [
    dict(stock_code="300750", name="宁德时代", aliases=["宁王"], products=["麒麟电池", "神行超充电池"], technologies=["CTP"]),
    dict(stock_code="600519", name="贵州茅台", aliases=["茅台"], products=["飞天茅台", "白酒"], brands=["i茅台"]),
    dict(stock_code="002594", name="比亚迪", products=["刀片电池"], technologies=["DM-i"]),
    dict(stock_code="600721", name="*ST百花"),
    dict(stock_code="300785", name="值得买"),
    dict(stock_code="001872", name="招商港口", aliases=["蛇口"], products=["高速公路"], enriched_at=None),
    dict(stock_code="000088", name="盐田港", aliases=["盐田"], ambiguous=["盐田"]),
    dict(stock_code="000058", name="深赛格", aliases=["赛格电子"]),
    dict(stock_code="000068", name="华控赛格", aliases=["赛格电子"]),
]


def seed_kb():
    with mdb.session_scope() as s:
        for c in COMPANIES:
            s.add(AShareCompany(
                stock_code=c["stock_code"], name=c["name"], aliases=dumps(c.get("aliases", [])),
                products=dumps(c.get("products", [])), technologies=dumps(c.get("technologies", [])),
                brands=dumps(c.get("brands", [])), ambiguous_terms=dumps(c.get("ambiguous", [])),
                manual_locked=False, enriched_at=c.get("enriched_at", datetime(2026, 11, 1)),
            ))


def matcher_from_kb():
    with mdb.session_scope() as s:
        return CompanyMatcher(list(s.scalars(select(AShareCompany))))


def rec(aid, desc, ts, likes=100, sec_uid="u1", **kw):
    return VideoRecord(aweme_id=aid, desc=desc, create_time=ts, sec_uid=sec_uid, nickname=f"博主{sec_uid}",
                       liked_count=likes, comment_count=10, share_count=5, collected_count=8,
                       video_url=f"https://v/{aid}.mp4", source="fake", **kw)


class FakeDiscovery(DiscoverySource):
    name = "fake"

    def __init__(self, by_kw, creator_posts=None):
        self.by_kw, self.creator_posts = by_kw, creator_posts or {}
        self.creator_calls = []

    def search_recent(self, keywords, publish_days=1):
        self.publish_days = publish_days
        return {k: self.by_kw.get(k, []) for k in keywords}

    def creator_recent_posts(self, sec_uids, since_ts):
        self.creator_calls.append(list(sec_uids))
        return {u: self.creator_posts.get(u, []) for u in sec_uids}


class FakeTracking(TrackingSource):
    name = "fake"

    def __init__(self):
        self.data = {}

    def fetch_videos(self, aweme_ids):
        return {a: self.data[a] for a in aweme_ids if a in self.data}


class FakeLLM:
    """粗筛：含「股」「茅台」「电池」即相关；精判：按文字里的公司给出结果。"""

    def __init__(self):
        self.judge_calls = 0

    def complete_json(self, system, user):
        if "results" in system:  # 粗筛
            out = []
            for line in user.splitlines():
                i = int(line[1 : line.index("]")])
                out.append({"i": i, "related": any(k in line for k in ("股", "茅台", "电池"))})
            return {"results": out}
        self.judge_calls += 1
        companies = []
        if "麒麟电池" in user or "宁王" in user:
            stance = "bearish" if "看空" in user else "bullish"
            companies.append({"code": "300750", "name": "宁德时代", "basis": "product", "stance": stance, "evidence": "麒麟电池"})
        if "茅台" in user:
            companies.append({"code": "", "name": "贵州茅台", "basis": "name", "stance": "neutral", "evidence": "茅台"})
        if "华为" in user:
            companies.append({"code": "999999", "name": "华为", "basis": "name", "stance": "bullish"})  # 应被丢弃
        return {"is_company_analysis": bool(companies), "confidence": 0.9 if companies else 0.2,
                "summary": "测试", "companies": companies}


def noop_analyzer(s):
    return MediaAnalyzer(s, transcriber=NoopTranscriber(), frame_reader=NoopFrameReader())


# ---------------------------------------------------------------------- 单元测试
def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('结果如下：{"a": [1,2]} 以上') == {"a": [1, 2]}
    assert extract_json("[1, 2]") == [1, 2]
    with pytest.raises(ValueError):
        extract_json("没有 JSON")


def test_name_variants():
    assert set(name_variants("*ST百花")) == {"*ST百花", "百花"}
    assert "万科" in name_variants("万  科Ａ")


def test_matcher_bases_and_generic_terms(settings):
    seed_kb()
    m = matcher_from_kb()
    hits = m.match("宁王的麒麟电池又要涨？顺便聊聊 600519 和比亚迪的电池")
    assert hits["300750"].bases == {"name", "product"}
    assert "code" in hits["600519"].bases
    assert hits["002594"].bases == {"name"}  # 「电池」是通用词，不会让比亚迪命中 product
    assert "百花" in m.match("百花医药今天涨停")["600721"].terms
    assert "600519" not in m.match("白酒板块整体走弱")  # 「白酒」被过滤
    assert m.resolve("SZ300750", None) == ("300750", "宁德时代")
    assert m.resolve(None, "茅台") == ("600519", "贵州茅台")
    assert m.resolve("999999", "华为") is None


def test_matcher_strong_and_weak_terms(settings):
    seed_kb()
    m = matcher_from_kb()
    assert m.match("宁德时代业绩超预期")["300750"].strong
    assert m.match("600519")["600519"].strong
    assert not m.match("这个值得买吗")["300785"].strong          # 简称是日常用语
    assert not m.match("宁王来了")["300750"].strong               # 2 字外号只是弱候选
    assert not m.match("麒麟电池真香")["300750"].strong           # 产品命中只是弱候选
    assert "001872" not in m.match("高速公路堵车")                # 未扩充公司的业务板块不参与匹配
    assert m.match("盐田港今天涨停")["000088"].strong              # 完整简称是强词
    assert not m.match("周末去盐田玩")["000088"].strong            # 大模型标为日常用语的外号是弱词
    shared = m.match("赛格电子大楼")
    assert {"000058", "000068"} <= set(shared) and not any(h.strong for h in shared.values())  # 多家共用的外号
    assert m.resolve(None, "赛格电子") is None                   # 有歧义不强行指定


def test_create_tables_adds_missing_column(tmp_path):
    from sqlalchemy import inspect, text
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE a_share_company (stock_code VARCHAR(10) PRIMARY KEY, name VARCHAR(64))"))
    mdb.create_tables(engine)
    assert "ambiguous_terms" in {c["name"] for c in inspect(engine).get_columns("a_share_company")}
    mdb.create_tables(engine)  # 重复执行不报错


def test_enrich_replaces_products_and_records_ambiguous(settings):
    from StockVideoMonitor.knowledge_base import enrich_with_llm

    with mdb.session_scope() as s:
        s.add(AShareCompany(stock_code="000088", name="盐田港", aliases=dumps(["旧称"]), products=dumps(["高速公路收费", "仓储服务"]),
                            main_business="港口装卸", manual_locked=False))

    class EnrichLLM:
        def complete_json(self, system, user):
            assert "高速公路收费" in user  # 旧数据里的业务板块仍作为背景材料提供给大模型
            return {"companies": [{"code": "88", "aliases": ["盐田"], "products": ["盐田国际集装箱码头", "芯片"],
                                   "technologies": [], "brands": [], "ambiguous_terms": ["盐田"]}]}

    assert enrich_with_llm(EnrichLLM(), refresh=True) == 1
    with mdb.session_scope() as s:
        c = s.get(AShareCompany, "000088")
        assert json.loads(c.aliases) == ["旧称", "盐田"]
        assert json.loads(c.products) == ["盐田国际集装箱码头"]  # 替换而非合并，且过滤通用词
        assert json.loads(c.ambiguous_terms) == ["盐田"] and c.enriched_at is not None


def test_cleanup_removes_leaked_and_foreign_official_names(settings):
    from StockVideoMonitor.knowledge_base import cleanup_knowledge_base

    with mdb.session_scope() as s:
        s.add(AShareCompany(stock_code="300750", name="宁德时代", aliases=dumps(["宁王"]), manual_locked=False))
        s.add(AShareCompany(stock_code="600519", name="贵州茅台", aliases=dumps(["茅台", "宁王"]),
                            products=dumps(["飞天茅台", "刀片电池"]), manual_locked=False))
        s.add(AShareCompany(stock_code="000001", name="平安银行", manual_locked=False))
        s.add(AShareCompany(stock_code="601318", name="中国平安", aliases=dumps(["平安"]),
                            brands=dumps(["平安银行", "平安好医生"]), manual_locked=False))
        s.add(AShareCompany(stock_code="002594", name="比亚迪", aliases=dumps(["宁王"]), manual_locked=True))
    assert cleanup_knowledge_base() == 2
    with mdb.session_scope() as s:
        assert json.loads(s.get(AShareCompany, "600519").aliases) == ["茅台"]
        assert json.loads(s.get(AShareCompany, "600519").products) == ["飞天茅台"]
        assert json.loads(s.get(AShareCompany, "601318").brands) == ["平安好医生"]
        assert json.loads(s.get(AShareCompany, "300750").aliases) == ["宁王"]
        assert json.loads(s.get(AShareCompany, "002594").aliases) == ["宁王"]  # 人工维护的不动


def test_own_official_name_not_ambiguous(settings):
    with mdb.session_scope() as s:
        s.add(AShareCompany(stock_code="002594", name="比亚迪", ambiguous_terms=dumps(["比亚迪"]),
                            enriched_at=datetime(2026, 11, 1), manual_locked=False))
        s.add(AShareCompany(stock_code="600630", name="龙头股份", aliases=dumps(["龙头"]), manual_locked=False))
    m = matcher_from_kb()
    assert m.match("比亚迪销量创新高")["002594"].strong
    assert "600630" not in m.match("券商龙头")  # 「龙头」是通用词


def test_classifier_drops_unknown_and_applies_threshold(settings):
    seed_kb()
    clf = TwoStageClassifier(matcher_from_kb(), FakeLLM(), min_confidence=0.6)
    r = clf.judge("华为新品发布，麒麟电池供应链受益")
    assert r.is_company_analysis and [c.stock_code for c in r.companies] == ["300750"]
    assert r.companies[0].basis == "product"
    r2 = clf._parse_judge({"is_company_analysis": True, "confidence": 0.5,
                           "companies": [{"code": "600519", "name": "贵州茅台"}]}, {})
    assert not r2.is_company_analysis  # 置信度不够
    assert clf.prefilter(["今天聊聊茅台", "猫咪日常", "600519 走势"]) == [True, False, True]


def test_daily_deltas_handles_gaps():
    d0 = date(2026, 11, 1)
    snaps = [(d0, {"liked_count": 100, "share_count": 0, "collected_count": 0, "comment_count": 0}),
             (d0 + timedelta(days=2), {"liked_count": 300, "share_count": 10, "collected_count": 0, "comment_count": 0})]
    deltas = daily_deltas(snaps)
    assert deltas[0][1]["liked_count"] == 100
    assert deltas[1][1]["liked_count"] == 100  # 两天增长 200，平均每天 100
    assert deltas[1][1]["share_count"] == 5


def test_align_schema_noop_on_non_postgres(settings):
    from StockVideoMonitor.sources.mediacrawler import align_mediacrawler_schema
    assert align_mediacrawler_schema(mdb.get_engine()) == []  # SQLite / MySQL 不需要改


def test_set_config_lines(tmp_path):
    p = tmp_path / "cfg.py"
    p.write_text('PLATFORM = "xhs"\nKEYWORDS = (\n  "a"\n)\nOTHER = 1\nENABLE_GET_COMMENTS = True\n', encoding="utf-8")
    _set_config_lines(p, {"PLATFORM": '"dy"', "KEYWORDS": "'股票,A股'", "ENABLE_GET_COMMENTS": "False"})
    assert p.read_text(encoding="utf-8") == "PLATFORM = \"dy\"\nKEYWORDS = '股票,A股'\nOTHER = 1\nENABLE_GET_COMMENTS = False\n"


# ---------------------------------------------------------------------- TikHub 客户端
def aweme(aid, desc="", likes=1, collects=2, ts=1_762_000_000, aweme_type=0):
    return {"aweme_id": aid, "desc": desc, "create_time": ts, "aweme_type": aweme_type,
            "author": {"uid": 1, "sec_uid": "sec-" + aid, "nickname": "n", "follower_count": 9},
            "statistics": {"digg_count": likes, "comment_count": 3, "share_count": 4, "collect_count": collects},
            "video": {"play_addr": {"url_list": ["https://play/" + aid]}, "cover": {"url_list": ["c"]}, "duration": 15000},
            "text_extra": [{"hashtag_name": "股票"}, {"hashtag_name": "股票"}, {"user_id": "x"}]}


def test_parse_aweme_fields():
    r = th.parse_aweme(aweme("1", "看好 #股票", likes=10, collects=7))
    assert (r.liked_count, r.collected_count, r.share_count, r.comment_count) == (10, 7, 4, 3)
    assert r.hashtags == ["股票"] and r.video_url == "https://play/1" and r.sec_uid == "sec-1"
    assert r.duration_ms == 15000


def test_parse_image_post():
    d = aweme("p1", "茅台年报图解")
    d["aweme_type"] = 68
    d["image_post_info"] = {"images": [{"display_image": {"url_list": ["https://img/1.jpg"]}},
                                       {"url_list": ["https://img/2.jpg"]}, {"url_list": ["https://img/2.jpg"]}]}
    r = th.parse_aweme(d)
    assert r.is_image_post and r.image_urls == ["https://img/1.jpg", "https://img/2.jpg"]
    assert r.video_url is None and r.aweme_url == "https://www.douyin.com/note/p1"
    d2 = aweme("p2")
    d2["images"] = [{"url_list": ["https://img/a.jpg"]}]
    assert th.parse_aweme(d2).image_urls == ["https://img/a.jpg"]


def test_analyze_images_ocr(settings, monkeypatch):
    class FakeReader:
        def read(self, paths):
            return "\n".join(p.name for p in paths)

    analyzer = MediaAnalyzer(settings, transcriber=NoopTranscriber(), frame_reader=FakeReader())

    def fake_download(url, dest):
        if "bad" in url:
            raise RuntimeError("404")
        dest.write_bytes(b"x")
        return dest

    monkeypatch.setattr(analyzer, "download", fake_download)
    text = analyzer.analyze_images("p1", ["https://img/1.jpg", "https://img/bad.jpg", "https://img/3.jpg"])
    assert text == "img_000.jpg\nimg_002.jpg"  # 单张失败不影响其他
    assert not any(Path(settings.STOCK_VIDEO_MEDIA_DIR).iterdir())  # 临时文件已清理


def test_tikhub_search_pagination_and_batch(settings, monkeypatch):
    monkeypatch.setattr(th.time, "sleep", lambda *_: None)
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path, request.content))
        assert request.headers["Authorization"] == "Bearer test-token"
        if request.url.path == th.SEARCH_PATH:
            body = json.loads(request.content)
            assert body["sort_type"] == "2" and body["publish_time"] == "1" and body["content_type"] == "0"
            if body["cursor"] == 0:
                data = {"business_data": [{"data": {"aweme_info": aweme("a1")}}, {"data": {"aweme_info": aweme("a2")}}],
                        "cursor": 10, "has_more": 1, "search_id": "sid"}
            else:
                assert body["search_id"] == "sid"
                data = {"business_data": [{"data": {"aweme_info": aweme("a2")}}, {"data": {"aweme_info": aweme("a3")}}],
                        "cursor": 20, "has_more": 0}
            return httpx.Response(200, json={"code": 200, "data": data})
        if request.url.path == th.MULTI_VIDEO_PATH:
            ids = json.loads(request.content)
            assert isinstance(ids, list) and len(ids) <= 50
            if len(calls) == 3 and not hasattr(handler, "failed"):
                handler.failed = True
                return httpx.Response(429)
            return httpx.Response(200, json={"code": 200, "data": {"aweme_details": [aweme(i) for i in ids if i != "x7"]}})
        return httpx.Response(404)

    client = th.TikHubClient(settings, transport=httpx.MockTransport(handler))
    res = client.search_videos("股票", pages=3)
    assert [r.aweme_id for r in res] == ["a1", "a2", "a2", "a3"]  # 去重在 merge_records 里做
    ids = [f"x{i}" for i in range(60)]
    got = client.multi_video(ids)
    assert len(got) == 59 and "x7" not in got  # 查不到的视频不在结果里
    multi_calls = [c for c in calls if c[1] == th.MULTI_VIDEO_PATH]
    assert len(multi_calls) == 3  # 50 + 10，其中一次 429 重试
    assert client.calls == 4


def test_tikhub_error_code(settings):
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"code": 400, "message": "bad"}))
    with pytest.raises(th.SourceError):
        th.TikHubClient(settings, transport=t).request("GET", "/x")


# ---------------------------------------------------------------------- 端到端流程
def test_end_to_end_daily_cycle(settings):
    seed_kb()
    day0 = datetime(2026, 11, 3, 7, 30)
    ts = int((day0 - timedelta(hours=3)).timestamp())
    old_ts = int((day0 - timedelta(days=5)).timestamp())
    disc = FakeDiscovery({
        "股票": [rec("v1", "宁王的麒麟电池要起飞了，A股新能源龙头分析", ts, likes=500),
                 rec("v2", "股市今天大盘震荡，明天怎么走", ts, sec_uid="u2"),
                 rec("v3", "猫咪日常", ts, sec_uid="u3"),
                 rec("old", "宁王老视频", old_ts)],
        "A股": [rec("v1", "宁王的麒麟电池要起飞了，A股新能源龙头分析", ts, likes=500),
                rec("img", "茅台图文", ts, aweme_type=68)],
    })
    tracking = FakeTracking()
    tracking.data["v1"] = rec("v1", "", ts, likes=800)
    tracking.data["v2"] = rec("v2", "", ts, likes=50, sec_uid="u2")
    tracking.data["img"] = rec("img", "", ts, likes=300, aweme_type=68)
    llm = FakeLLM()
    clf = TwoStageClassifier(matcher_from_kb(), llm, min_confidence=0.6)
    stats = DailyDiscovery(disc, clf, noop_analyzer(settings), tracking, settings, hot_keywords_fn=lambda n: []).run(day0)

    assert stats.candidates == 4  # 去重后 v1/v2/v3/img；old 太旧被排除
    assert (stats.new_videos, stats.finance, stats.flagged) == (4, 3, 2)
    with mdb.session_scope() as s:
        v1 = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == "v1"))
        assert v1.is_tracked and v1.tracking_status == "active"
        assert v1.track_until == day0 + timedelta(days=14)
        assert v1.liked_count == 800  # 用刷新后的数据
        assert sorted(json.loads(v1.discovery_channels)) == ["keyword:A股", "keyword:股票"]
        v2 = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == "v2"))
        assert v2.stage == "analyzed" and not v2.is_company_analysis and not v2.is_tracked
        v3 = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == "v3"))
        assert v3.stage == "prefilter_rejected"
        mention = s.scalar(select(DouyinStockVideoCompany).where(DouyinStockVideoCompany.aweme_id == "v1"))
        assert (mention.stock_code, mention.basis, mention.stance) == ("300750", "product", "bullish")
        assert s.get(DouyinFinanceCreator, "u1").flagged_video_count == 2  # v1 + 同一作者的图文 img
        assert s.scalar(select(DouyinStockVideoSnapshot).where(DouyinStockVideoSnapshot.aweme_id == "v1")).liked_count == 800
        img = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == "img"))
        assert img.is_image_post and img.aweme_type == 68 and img.is_tracked  # 图文作品同样识别并跟踪
        assert img.aweme_url == "https://www.douyin.com/note/img"
        assert s.scalar(select(DouyinStockVideoCompany).where(DouyinStockVideoCompany.aweme_id == "img")).stock_code == "600519"

    # 同一天再跑：不会重复入库，tracker 跳过当天已拍快照的视频
    stats_again = DailyDiscovery(disc, clf, noop_analyzer(settings), tracking, settings, hot_keywords_fn=lambda n: []).run(day0)
    assert stats_again.new_videos == 0
    assert DailyTracker(tracking, settings).run(day0).active == 0

    # 第 1~3 天平稳增长，第 4 天爆发
    engine = AlertEngine(settings)
    likes = 800
    for i, inc in enumerate([300, 250, 200, 5000], start=1):
        likes += inc
        tracking.data["v1"] = VideoRecord(aweme_id="v1", liked_count=likes, comment_count=10,
                                          share_count=5 + i, collected_count=8 + i)
        t = DailyTracker(tracking, settings).run(day0 + timedelta(days=i))
        assert t.updated == 2  # v1 + 图文 img
        alerts = engine.evaluate((day0 + timedelta(days=i)).date())
        spikes = [a for a in alerts if a.alert_type == "spike"]
        if i < 4:
            assert not spikes
        else:
            assert len(spikes) == 1 and "宁德时代(300750)" in spikes[0].title
            assert spikes[0].detail["spikes"]["liked_count"]["today"] == 5000
            assert len(engine.save(alerts)) == len(alerts)
            assert engine.save(alerts) == []  # 去重

    # 博主池：第二天发现时会拉取 u1 的作品
    disc.creator_posts = {"u1": [rec("v9", "宁王看空，麒麟电池价格战", int((day0 + timedelta(days=5)).timestamp()))]}
    tracking.data["v9"] = rec("v9", "", 0, likes=10)
    stats5 = DailyDiscovery(disc, clf, noop_analyzer(settings), tracking, settings, hot_keywords_fn=lambda n: []).run(day0 + timedelta(days=5, hours=1))
    assert disc.creator_calls[-1] == ["u1"] and stats5.channel_counts["creator"] == 1 and stats5.flagged == 1

    # 连续两次查不到 → 不可见
    tracking.data.pop("v1")
    DailyTracker(tracking, settings).run(day0 + timedelta(days=6))
    t7 = DailyTracker(tracking, settings).run(day0 + timedelta(days=7))
    assert t7.unavailable_ids == ["v1"]
    alerts = engine.evaluate((day0 + timedelta(days=7)).date(), t7.unavailable_ids)
    assert any(a.alert_type == "unavailable" and a.aweme_id == "v1" for a in alerts)

    # v9 跟踪 14 天后到期
    t_end = DailyTracker(tracking, settings).run(day0 + timedelta(days=5, hours=1) + timedelta(days=15))
    assert t_end.expired == 2  # v9 与 img 都已过 14 天
    with mdb.session_scope() as s:
        assert s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == "v9")).tracking_status == "expired"


def test_company_level_alerts(settings):
    engine = AlertEngine(settings)
    today = date(2026, 11, 10)
    past = {today - timedelta(days=i): 1 for i in range(1, 8)}
    a = engine.company_alerts("300750", "宁德时代", {**past, today: 6}, {today: 50000, **{d: 5000 for d in past}},
                              {today: {"bearish": 4, "bullish": 1}, **{d: {"bullish": 1} for d in past}}, today)
    types = {x.alert_type for x in a}
    assert types == {"video_surge", "interaction_surge", "stance_shift"}
    assert "转空" in next(x.title for x in a if x.alert_type == "stance_shift")
    quiet = engine.company_alerts("300750", "宁德时代", {**past, today: 2}, {today: 6000}, {}, today)
    assert quiet == []


def test_company_alerts_from_db(settings):
    """5 条新视频同一天讨论同一家公司 → 公司级预警（已过预热期）。"""
    settings.STOCK_VIDEO_ALERT_WARMUP_DAYS = 0
    seed_kb()
    day = datetime(2026, 11, 3, 7, 30)
    ts = int((day - timedelta(hours=2)).timestamp())
    disc = FakeDiscovery({"股票": [rec(f"m{i}", f"股票分析：茅台第{i}弹", ts, sec_uid=f"c{i}") for i in range(5)]})
    tracking = FakeTracking()
    for i in range(5):
        tracking.data[f"m{i}"] = rec(f"m{i}", "", ts, likes=4000, sec_uid=f"c{i}")
    clf = TwoStageClassifier(matcher_from_kb(), FakeLLM())
    DailyDiscovery(disc, clf, noop_analyzer(settings), tracking, settings, hot_keywords_fn=lambda n: []).run(day)
    alerts = AlertEngine(settings).evaluate(day.date())
    company = [a for a in alerts if a.level == "company"]
    assert {a.alert_type for a in company} == {"video_surge", "interaction_surge"}
    assert all(a.stock_code == "600519" for a in company)
    AlertEngine.save(alerts)
    with mdb.session_scope() as s:
        assert len(list(s.scalars(select(StockVideoAlert)))) == len(alerts)


def test_split_command_windows_paths(monkeypatch):
    monkeypatch.setattr(media_mod.os, "name", "nt")
    cmd = media_mod.split_command(r'"C:\Program Files\analyzer\analyzer.exe" transcribe {input}', Path("a.wav"))
    assert cmd == [r"C:\Program Files\analyzer\analyzer.exe", "transcribe", "a.wav"]
    monkeypatch.setattr(media_mod.os, "name", "posix")
    assert media_mod.split_command("analyzer --in '{input}'", Path("b.wav")) == ["analyzer", "--in", "b.wav"]


# ---------------------------------------------------------------------- 稳定性：超时、重试、断点续跑
def test_tikhub_retries_retryable_400(settings, monkeypatch):
    monkeypatch.setattr(th.time, "sleep", lambda *_: None)
    n = {"calls": 0}

    def handler(request):
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(400, json={"detail": {"message": "Request failed. Please retry. You won't be charged for this request."}})
        return httpx.Response(200, json={"code": 200, "data": {"ok": 1}})

    client = th.TikHubClient(settings, transport=httpx.MockTransport(handler))
    assert client.request("GET", "/x") == {"ok": 1} and n["calls"] == 2
    bad = th.TikHubClient(settings, transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"detail": "bad param"})))
    with pytest.raises(th.SourceError):
        bad.request("GET", "/x")  # 普通 400 不重试


def test_ocr_subprocess_timeout_and_success(monkeypatch, tmp_path):
    import subprocess
    reader = media_mod.SubprocessOCRFrameReader("rapidocr", timeout=5)
    img = tmp_path / "a.jpg"
    img.write_bytes(b"x")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ocr", timeout=5)

    monkeypatch.setattr(media_mod.subprocess, "run", hang)
    assert reader.read([img]) == ""  # 超时不抛异常，返回空

    class R:
        returncode, stderr = 0, b""
        stdout = json.dumps(["茅台 600519", "茅台 600519", "x"], ensure_ascii=False).encode("utf-8")

    monkeypatch.setattr(media_mod.subprocess, "run", lambda *a, **k: R())
    assert reader.read([img]) == "茅台 600519"  # 去重并过滤过短的行


def test_download_size_limit(settings, monkeypatch, tmp_path):
    from contextlib import contextmanager

    class Resp:
        def raise_for_status(self): pass
        def iter_bytes(self, n):
            for _ in range(10):
                yield b"x" * (1024 * 1024)

    @contextmanager
    def fake_stream(*a, **k):
        yield Resp()

    s = make_settings(tmp_path, STOCK_VIDEO_MAX_DOWNLOAD_MB=3)
    analyzer = MediaAnalyzer(s, transcriber=NoopTranscriber(), frame_reader=NoopFrameReader())
    monkeypatch.setattr(media_mod.httpx, "stream", fake_stream)
    with pytest.raises(RuntimeError, match="3MB"):
        analyzer.download("https://v/x.mp4", tmp_path / "x.mp4")


def test_resume_unfinished_and_time_budget(settings):
    """上次中断（stage=finance）的作品下次继续精判；超过时间预算后不再下载解析。"""
    seed_kb()
    day = datetime(2026, 11, 5, 17, 0)
    ts = int((day - timedelta(hours=2)).timestamp())
    with mdb.session_scope() as s:
        s.add(DouyinStockVideo(aweme_id="r1", desc="茅台深度分析", create_time=ts, discovered_at=day,
                               stage="finance", finance_related=True, is_tracked=False, tracking_status="none", miss_count=0))

    class CountingAnalyzer(MediaAnalyzer):
        calls = 0
        def analyze(self, aid, url):
            CountingAnalyzer.calls += 1
            return "", ""

    analyzer = CountingAnalyzer(settings, transcriber=NoopTranscriber(), frame_reader=NoopFrameReader())
    disc = FakeDiscovery({"股票": [rec("n1", "宁王麒麟电池股票分析", ts)]})
    clf = TwoStageClassifier(matcher_from_kb(), FakeLLM())
    stats = DailyDiscovery(disc, clf, analyzer, FakeTracking(), settings, hot_keywords_fn=lambda n: []).run(day + timedelta(hours=1))
    assert stats.flagged == 2 and CountingAnalyzer.calls == 2  # r1 续跑 + n1 新增
    with mdb.session_scope() as s:
        assert s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == "r1")).stage == "analyzed"

    budget0 = make_settings(Path(settings.STOCK_VIDEO_MEDIA_DIR).parent, STOCK_VIDEO_ANALYSIS_BUDGET_MINUTES=0)
    disc2 = FakeDiscovery({"股票": [rec("n2", "茅台股票分析", ts)]})
    CountingAnalyzer.calls = 0
    st2 = DailyDiscovery(disc2, clf, analyzer, FakeTracking(), budget0, hot_keywords_fn=lambda n: []).run(day + timedelta(hours=2))
    assert st2.flagged == 1 and CountingAnalyzer.calls == 0  # 超预算：只用文字判断


def test_company_alerts_warmup_and_hot_only(settings):
    seed_kb()
    day = datetime(2026, 11, 6, 7, 0)
    ts = int((day - timedelta(hours=2)).timestamp())
    # 5 条只通过人气榜名字搜到的茅台视频 + 1 条关键词搜到的
    disc = FakeDiscovery({"股票": [rec("k1", "股票分析：茅台", ts, sec_uid="c0")],
                          "茅台": [rec(f"h{i}", f"茅台第{i}弹分析", ts, sec_uid=f"c{i}") for i in range(1, 6)]})
    tracking = FakeTracking()
    clf = TwoStageClassifier(matcher_from_kb(), FakeLLM())
    dd = DailyDiscovery(disc, clf, noop_analyzer(settings), tracking, settings, hot_keywords_fn=lambda n: ["茅台"])
    dd.run(day)
    engine = AlertEngine(settings)
    assert [a for a in engine.evaluate(day.date()) if a.level == "company"] == []  # 预热期内不出公司级预警
    settings.STOCK_VIDEO_ALERT_WARMUP_DAYS = 0
    company = [a for a in engine.evaluate(day.date()) if a.level == "company"]
    assert not any(a.alert_type == "video_surge" for a in company)  # 只计 1 条（人气榜搜到的 5 条不计），未达阈值


def test_tikhub_keeps_earlier_pages_when_later_page_fails(settings, monkeypatch):
    monkeypatch.setattr(th.time, "sleep", lambda *_: None)

    def handler(request):
        body = json.loads(request.content)
        if body["cursor"] == 0:
            return httpx.Response(200, json={"code": 200, "data": {
                "business_data": [{"data": {"aweme_info": aweme("p1")}}], "cursor": 10, "has_more": 1,
                "extra": {"search_request_id": "req-1"}}})
        assert body["search_id"] == "req-1"  # 从 search_request_id 取到翻页用的 search_id
        return httpx.Response(400, json={"detail": "bad"})

    client = th.TikHubClient(settings, transport=httpx.MockTransport(handler))
    assert [r.aweme_id for r in client.search_videos("股票", pages=3)] == ["p1"]


def test_daily_report_workbook(settings, tmp_path):
    import openpyxl
    from StockVideoMonitor.report import company_sort_key, generate_report, latest_report, report_name

    assert sorted(["中国平安", "比亚迪", "贵州茅台", "宁德时代"], key=company_sort_key) == ["比亚迪", "贵州茅台", "宁德时代", "中国平安"]
    seed_kb()
    day0 = datetime(2026, 11, 6, 7, 0)
    ts = int((day0 - timedelta(hours=2)).timestamp())
    disc = FakeDiscovery({"股票": [rec("v1", "宁王麒麟电池股票分析", ts, likes=500),
                                   rec("v2", "茅台股票深度分析", ts, sec_uid="u2", likes=900),
                                   rec("v3", "猫咪日常", ts, sec_uid="u3")]})
    tracking = FakeTracking()
    clf = TwoStageClassifier(matcher_from_kb(), FakeLLM())
    DailyDiscovery(disc, clf, noop_analyzer(settings), tracking, settings, hot_keywords_fn=lambda n: []).run(day0)
    tracking.data["v1"] = VideoRecord(aweme_id="v1", liked_count=1500, comment_count=20, share_count=9, collected_count=30)
    tracking.data["v2"] = VideoRecord(aweme_id="v2", liked_count=950, comment_count=11, share_count=5, collected_count=8)
    day1 = day0 + timedelta(days=1)
    DailyTracker(tracking, settings).run(day1)

    first = generate_report(day0.date(), out_dir=tmp_path)
    assert first.name == report_name(day0.date()) == "抖音上市公司分析视频跟踪_20261106.xlsx"
    path = generate_report(day1.date(), out_dir=tmp_path)
    # 只保留一份：新日期的文件生成后，旧日期的那份被移除（相当于改名）
    assert [p.name for p in tmp_path.glob("*.xlsx")] == ["抖音上市公司分析视频跟踪_20261107.xlsx"]
    assert latest_report(tmp_path) == path and not (tmp_path / "history").exists()
    wb = openpyxl.load_workbook(path)
    assert wb.sheetnames == ["跟踪总表", "每日数据", "公司汇总", "预警记录", "说明"]
    ws = wb["跟踪总表"]
    header = [c.value for c in ws[1]]
    rows = [dict(zip(header, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    assert [r["公司名称"] for r in rows] == ["贵州茅台", "宁德时代"]  # 按拼音：G < N
    ning = rows[1]
    assert (ning["点赞"], ning["点赞日增"], ning["收藏日增"], ning["跟踪状态"]) == (1500, 1000, 22, "跟踪中")
    assert ning["已跟踪天数"] == 2 and ws.cell(row=3, column=header.index("作品链接") + 1).hyperlink is not None
    daily = list(wb["每日数据"].iter_rows(min_row=2, values_only=True))
    assert len(daily) == 4  # 2 条作品 × 2 天
    summary = {r[1]: r for r in wb["公司汇总"].iter_rows(min_row=2, values_only=True)}
    assert summary["300750"][2] == 1 and summary["300750"][10] == 1000


def test_scheduled_run_skips_late_start(settings):
    from StockVideoMonitor.pipeline import finish_deadline, run_daily

    late = datetime(2026, 11, 9, 6, 55)
    assert finish_deadline(late, settings) == datetime(2026, 11, 9, 6, 50)
    assert run_daily(now=late, settings=settings, enforce_deadline=True)["skipped"] == "late_start"


def test_hard_deadline_defers_remaining_and_lookback_widens(settings):
    """到截止时间就停，剩余作品保持 finance 留到下次；漏跑后发现窗口自动扩大并改搜最近一周。"""
    seed_kb()
    day = datetime(2026, 11, 9, 5, 0)
    ts = int((day - timedelta(hours=2)).timestamp())
    with mdb.session_scope() as s:  # 上次发现是 3 天前（周末电脑关机）
        s.add(DouyinStockVideo(aweme_id="old", desc="旧视频", create_time=ts - 3 * 86400,
                               discovered_at=day - timedelta(days=3), stage="analyzed", finance_related=False,
                               is_tracked=False, tracking_status="none", miss_count=0))
    disc = FakeDiscovery({"股票": [rec("d1", "茅台股票分析", ts), rec("d2", "宁王麒麟电池股票分析", ts)]})
    analyzer = MediaAnalyzer(settings, transcriber=NoopTranscriber(), frame_reader=NoopFrameReader())
    clf = TwoStageClassifier(matcher_from_kb(), FakeLLM())
    dd = DailyDiscovery(disc, clf, analyzer, FakeTracking(), settings, hot_keywords_fn=lambda n: [])
    stats = dd.run(day, hard_deadline=datetime.now() - timedelta(seconds=1))
    assert stats.lookback_hours == 72 and disc.publish_days == 7
    assert stats.deferred == 2 and stats.flagged == 0
    with mdb.session_scope() as s:
        stages = {v.aweme_id: v.stage for v in s.scalars(select(DouyinStockVideo))}
    assert stages["d1"] == stages["d2"] == "finance"
    # 下次运行（无截止压力）继续处理，窗口恢复为 26 小时
    disc2 = FakeDiscovery({})
    st2 = DailyDiscovery(disc2, clf, analyzer, FakeTracking(), settings, hot_keywords_fn=lambda n: []).run(day + timedelta(hours=1))
    assert st2.flagged == 2 and st2.lookback_hours == 26 and disc2.publish_days == 1


def test_cleanup_files_and_old_rows(settings, tmp_path):
    import os
    import time
    from StockVideoMonitor import cleanup

    media = Path(settings.STOCK_VIDEO_MEDIA_DIR)
    (media / "123_abc").mkdir(parents=True)
    (media / "123_abc" / "video.mp4").write_bytes(b"x" * 1000)
    prof = tmp_path / "browser_data" / "dy_user_data_dir"
    (prof / "Default" / "Cache").mkdir(parents=True)
    (prof / "Default" / "Cache" / "data_1").write_bytes(b"x" * 500)
    (prof / "Default" / "Cookies").write_bytes(b"login")
    logs = tmp_path / "logs"
    logs.mkdir()
    old_log, new_log = logs / "stock_video_20260901.log", logs / "stock_video_20261106.log"
    old_log.write_text("old"); new_log.write_text("new")
    os.utime(old_log, (time.time() - 40 * 86400,) * 2)

    now = datetime(2026, 11, 6, 6, 0)
    with mdb.session_scope() as s:
        for aid, status, days in [("u_old", "none", 40), ("u_new", "none", 5), ("t_old", "expired", 40)]:
            s.add(DouyinStockVideo(aweme_id=aid, desc="x", discovered_at=now - timedelta(days=days), stage="analyzed",
                                   is_tracked=status != "none", tracking_status=status, miss_count=0))

    st = cleanup.CleanupStats()
    cleanup.clean_media_cache(settings, st)
    cleanup.clean_browser_cache(st, tmp_path / "browser_data")
    cleanup.clean_logs(settings, st, logs)
    cleanup.clean_database(settings, now, st)
    assert not any(media.iterdir())
    assert not (prof / "Default" / "Cache").exists() and (prof / "Default" / "Cookies").exists()
    assert not old_log.exists() and new_log.exists()
    with mdb.session_scope() as s:
        left = set(s.scalars(select(DouyinStockVideo.aweme_id)))
    assert left == {"u_new", "t_old"}


def test_hot_stock_names_fall_back_to_own_data(settings, monkeypatch):
    from StockVideoMonitor import knowledge_base as kb
    from StockVideoMonitor.models import DouyinStockVideoCompany

    def boom(n):
        raise ConnectionError("Connection reset by peer")

    monkeypatch.setattr(kb, "_hot_from_eastmoney", boom)
    monkeypatch.setattr(kb, "_hot_from_xueqiu", boom)
    ts = int(datetime.now().timestamp())
    with mdb.session_scope() as s:
        for i, name in enumerate(["贵州茅台", "贵州茅台", "宁德时代"]):
            s.add(DouyinStockVideoCompany(aweme_id=f"h{i}", stock_code="600519" if name == "贵州茅台" else "300750",
                                          stock_name=name, video_create_time=ts))
    assert kb.fetch_hot_stock_names(5) == ["贵州茅台", "宁德时代"]
    assert kb.HOT_SOURCE_USED == "近 7 天被标记最多的公司"


# ---------------------------------------------------------------------- 视频号
# 响应结构取自 2026-11-09 对 TikHub 视频号接口的真实请求（字段做了精简）
def wx_item(nonce, title, ts, author="财经博主"):
    return {"dateTime": "1小时前", "docID": f"finderobj{nonce}", "duration": "03:08",
            "exportId": f"export/{nonce}-{ts}", "image": f"https://findermp.video.qq.com/{nonce}.jpg",
            "jumpInfo": {"extInfo": json.dumps({"encryptedObjectId": f"export/{nonce}", "feedNonceId": nonce}), "jumpType": 9},
            "pubTime": ts, "showType": 1, "source": {"title": author}, "title": f'<em class="highlight">A股</em>{title}'}


def wx_search_page(items, cont=1):
    return {"code": 200, "data": {"keyword": "A股", "continue_flag": cont, "offset": 10, "cursor": "c1",
                                  "results": {"data": [{"subBoxes": [{"items": [it]} for it in items]}]}}}


def wx_obj(nonce, desc, ts, user="v2_abc@finder", fav=100, like=50, fwd=20, cmt=10, media_type=4):
    return {"id": int(nonce) + 1, "objectNonceId": f"{nonce}_0_0_0_0_0_x", "createtime": ts, "username": user,
            "nickname": "财经博主", "contact": {"username": user, "nickname": "财经博主", "fansCount": 0},
            "favCount": fav, "likeCount": like, "forwardCount": fwd, "commentCount": cmt,
            "objectDesc": {"description": desc, "mediaType": media_type,
                           "media": [{"url": "http://wxapp.tc.qq.com/v", "urlToken": "&t=1", "coverUrl": "https://wxapp.tc.qq.com/c",
                                      "coverUrlToken": "&t=2", "videoPlayLen": 188, "decodeKey": "1"}]}}


def test_wechat_parsers():
    from StockVideoMonitor.sources import wechat as wx

    r = wx.parse_search_item(wx_item("17102841814157420753", "茅台深度分析#股票 #A股", 1794229506, "湘妹子聊股"))
    assert (r.aweme_id, r.platform, r.nickname, r.duration_ms) == ("wx_17102841814157420753", "wechat", "湘妹子聊股", 188000)
    assert r.desc.startswith("A股茅台") and r.hashtags == ["股票", "A股"] and r.aweme_url == "" and r.export_id
    o = wx.parse_objects({"objects": [wx_obj("17102841814157420753", "茅台", 1794229506, fav=586, like=321, fwd=445, cmt=176)]})[0]
    assert o.aweme_id == r.aweme_id  # 搜索的 feedNonceId 与详情的 objectNonceId 第一段一致
    assert (o.liked_count, o.collected_count, o.share_count, o.comment_count) == (586, 321, 445, 176)
    assert o.sec_uid == "v2_abc@finder" and o.cover_url.endswith("&t=2") and not o.is_image_post
    img = wx.parse_object(wx_obj("5", "图文", 1, media_type=2))
    assert img.is_image_post and img.image_urls == ["http://wxapp.tc.qq.com/v&t=1"] and img.duration_ms is None


def test_wechat_end_to_end(settings, tmp_path, monkeypatch):
    import openpyxl
    from StockVideoMonitor.alerts import AlertEngine
    from StockVideoMonitor.report import generate_report
    from StockVideoMonitor.sources import wechat as wx

    monkeypatch.setattr(th.time, "sleep", lambda *_: None)
    seed_kb()
    day0 = datetime(2026, 11, 9, 5, 0)
    ts = int((day0 - timedelta(hours=3)).timestamp())
    state = {"day": 0}
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content or b"{}")
        calls.append(request.url.path)
        if request.url.path == wx.SEARCH_PATH:
            assert body["sort"] == "latest" and body["publish_time"] == "day"
            if body["keyword"] != "股票":
                return httpx.Response(200, json=wx_search_page([], cont=0))
            if not body.get("cursor"):
                return httpx.Response(200, json=wx_search_page([wx_item("111", "宁王麒麟电池股票分析", ts),
                                                                wx_item("222", "猫咪日常", ts)]))
            return httpx.Response(200, json=wx_search_page([wx_item("333", "茅台股票深度分析", ts)], cont=0))
        if request.url.path == wx.DETAIL_PATH:
            nonce = body["export_id"].split("/")[1].split("-")[0]
            desc = {"111": "宁王麒麟电池股票分析（完整文案）", "333": "茅台股票深度分析"}[nonce]
            return httpx.Response(200, json={"code": 200, "data": {"objects": [wx_obj(nonce, desc, ts, fav=100)]}})
        if request.url.path == wx.USER_VIDEOS_PATH:
            assert body["username"] == "v2_abc@finder"
            bump = 5000 if state["day"] else 0
            return httpx.Response(200, json={"code": 200, "data": {"object": [
                wx_obj("111", "宁王", ts, fav=100 + bump, like=60), wx_obj("333", "茅台", ts, fav=100 + bump, like=70),
                wx_obj("999", "旧作品", ts - 5 * 86400)], "lastBuffer": ""}})
        return httpx.Response(404)

    src = wx.WechatChannelsSource(wx.TikHubClient(settings, transport=httpx.MockTransport(handler)), settings)
    clf = TwoStageClassifier(matcher_from_kb(), FakeLLM())
    stats = DailyDiscovery(src, clf, noop_analyzer(settings), src, settings, hot_keywords_fn=lambda n: [],
                           platform="wechat").run(day0)
    assert (stats.candidates, stats.flagged) == (3, 2)
    assert calls.count(wx.DETAIL_PATH) == 2  # 只对标记的作品查详情
    with mdb.session_scope() as s:
        v = s.scalar(select(DouyinStockVideo).where(DouyinStockVideo.aweme_id == "wx_111"))
        assert (v.platform, v.sec_uid, v.object_id, v.desc) == ("wechat", "v2_abc@finder", "112", "宁王麒麟电池股票分析（完整文案）")
        assert s.get(DouyinFinanceCreator, "v2_abc@finder").platform == "wechat"

    # 次日跟踪：同一作者的两条作品只需一次作者列表请求
    state["day"] = 1
    calls.clear()
    day1 = day0 + timedelta(days=1)
    t = DailyTracker(src, settings, platform="wechat").run(day1)
    assert t.updated == 2 and calls == [wx.USER_VIDEOS_PATH]
    # 抖音的跟踪不会碰到视频号作品
    assert DailyTracker(FakeTracking(), settings).run(day1).active == 0

    alerts = AlertEngine(settings, platform="wechat").evaluate(day1.date())
    assert alerts and all(a.platform == "wechat" and a.title.startswith("[视频号]") for a in alerts)
    AlertEngine.save(alerts)
    assert AlertEngine(settings).evaluate(day1.date()) == []  # 抖音预警不含视频号作品

    path = generate_report(day1.date(), out_dir=tmp_path, platform="wechat")
    dy = generate_report(day1.date(), out_dir=tmp_path)
    assert path.name == "视频号上市公司分析视频跟踪_20261110.xlsx" and dy.name == "抖音上市公司分析视频跟踪_20261110.xlsx"
    ws = openpyxl.load_workbook(path)["跟踪总表"]
    header = [c.value for c in ws[1]]
    rows = [dict(zip(header, [c.value for c in r])) for r in ws.iter_rows(min_row=2)]
    assert "推荐" in header and "收藏" not in header
    assert {r["公司名称"] for r in rows} == {"宁德时代", "贵州茅台"} and all(r["点赞日增"] == 5000 for r in rows)
    assert openpyxl.load_workbook(dy)["跟踪总表"].max_row == 1  # 抖音表里没有视频号作品


def test_tikhub_retries_failed_keywords_once_more(settings, monkeypatch):
    monkeypatch.setattr(th.time, "sleep", lambda *_: None)
    tries = {"价值投资": 0}

    class Client:
        calls = 0

        def search_videos(self, kw, pages, publish_days=1):
            if kw == "价值投资":
                tries[kw] += 1
                if tries[kw] == 1:
                    raise th.SourceError("HTTP 400")
            return [rec(f"{kw}1", kw, 1)]

    out = th.TikHubSource(client=Client(), settings=settings).search_recent(["股票", "价值投资"])
    assert set(out) == {"股票", "价值投资"} and tries["价值投资"] == 2
