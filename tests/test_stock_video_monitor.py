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
]


def seed_kb():
    with mdb.session_scope() as s:
        for c in COMPANIES:
            s.add(AShareCompany(
                stock_code=c["stock_code"], name=c["name"], aliases=dumps(c.get("aliases", [])),
                products=dumps(c.get("products", [])), technologies=dumps(c.get("technologies", [])),
                brands=dumps(c.get("brands", [])), manual_locked=False,
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

    def search_recent(self, keywords):
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
    """5 条新视频同一天讨论同一家公司 → 公司级预警。"""
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
