# -*- coding: utf-8 -*-
"""
TikHub 抖音接口适配

用到的接口（路径取自 TikHub 官方 OpenAPI 规范）：
- POST /api/v1/douyin/search/fetch_video_search_v2      关键词搜索视频（支持「最新发布」「最近一天」筛选）
- GET  /api/v1/douyin/app/v3/fetch_user_post_videos     博主主页作品
- POST /api/v1/douyin/app/v3/fetch_multi_video_v2       批量视频详情，一次最多 50 条，固定 0.05 美元/次

认证：请求头 Authorization: Bearer {token}
注意：TikHub 的 Web 版与 App V3 版搜索接口已停用，只能用 Douyin-Search 系列。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Dict, Iterable, Iterator, List, Optional

import httpx
from loguru import logger

from ..settings import MonitorSettings, get_settings
from .base import IMAGE_AWEME_TYPES, DiscoverySource, SourceError, TrackingSource, VideoRecord

SEARCH_PATH = "/api/v1/douyin/search/fetch_video_search_v2"
USER_POSTS_PATH = "/api/v1/douyin/app/v3/fetch_user_post_videos"
MULTI_VIDEO_PATH = "/api/v1/douyin/app/v3/fetch_multi_video_v2"
MULTI_VIDEO_BATCH = 50


# ---------------------------------------------------------------------- 响应解析
def _first_url(node: Any) -> Optional[str]:
    if isinstance(node, dict):
        urls = node.get("url_list") or []
        return urls[0] if urls else None
    return None


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_aweme(d: Dict[str, Any], source: str = "tikhub") -> Optional[VideoRecord]:
    """把抖音原始 aweme 结构转成 VideoRecord；字段缺失时尽量容错。"""
    aweme_id = d.get("aweme_id") or d.get("aweme_id_str")
    if not aweme_id:
        return None
    author = d.get("author") or {}
    stats = d.get("statistics") or {}
    video = d.get("video") or {}
    hashtags = []
    for t in d.get("text_extra") or []:
        name = (t or {}).get("hashtag_name")
        if name and name not in hashtags:
            hashtags.append(name)
    duration = video.get("duration") or d.get("duration")
    image_urls = extract_image_urls(d)
    is_image = bool(image_urls) or _to_int(d.get("aweme_type")) in IMAGE_AWEME_TYPES
    return VideoRecord(
        aweme_id=str(aweme_id),
        desc=d.get("desc") or "",
        title=d.get("preview_title") or d.get("item_title") or "",
        hashtags=hashtags,
        create_time=_to_int(d.get("create_time")),
        author_uid=str(author.get("uid")) if author.get("uid") else None,
        sec_uid=author.get("sec_uid"),
        nickname=author.get("nickname"),
        follower_count=_to_int(author.get("follower_count")),
        liked_count=_to_int(stats.get("digg_count")),
        comment_count=_to_int(stats.get("comment_count")),
        share_count=_to_int(stats.get("share_count")),
        collected_count=_to_int(stats.get("collect_count")),
        # 图文作品的 play_addr 通常只是背景音乐轮播，没有解说，不作为视频地址
        video_url=None if is_image else (_first_url(video.get("play_addr")) or _first_url(video.get("download_addr"))),
        cover_url=_first_url(video.get("cover")) or _first_url(video.get("origin_cover")) or (image_urls[0] if image_urls else None),
        duration_ms=None if is_image else _to_int(duration),
        aweme_type=_to_int(d.get("aweme_type")),
        image_urls=image_urls,
        source=source,
    )


def extract_image_urls(d: Dict[str, Any]) -> List[str]:
    """图文作品的图片：兼容 images[] 与 image_post_info.images[] 两种结构，每张取一个地址。"""
    images = d.get("images") or (d.get("image_post_info") or {}).get("images") or []
    urls: List[str] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        url = _first_url(img.get("display_image")) or _first_url(img) or (img.get("download_url_list") or [None])[0]
        if url and url not in urls:
            urls.append(url)
    return urls


def iter_awemes(payload: Any) -> Iterator[Dict[str, Any]]:
    """在任意层级的响应里找出所有 aweme 结构（含 aweme_id 且含 desc/statistics 的字典）。"""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("aweme_id") and ("statistics" in node or "desc" in node):
                yield node
                continue
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def find_key(payload: Any, key: str) -> Any:
    """广度优先找第一个出现的键，用于取 cursor / has_more / search_id 等分页字段。"""
    queue = deque([payload])
    while queue:
        node = queue.popleft()
        if isinstance(node, dict):
            if key in node:
                return node[key]
            queue.extend(node.values())
        elif isinstance(node, list):
            queue.extend(node)
    return None


def parse_awemes(payload: Any, source: str = "tikhub") -> List[VideoRecord]:
    seen, out = set(), []
    for raw in iter_awemes(payload):
        rec = parse_aweme(raw, source)
        if rec and rec.aweme_id not in seen:
            seen.add(rec.aweme_id)
            out.append(rec)
    return out


# ---------------------------------------------------------------------- 客户端
class TikHubClient:
    def __init__(self, settings: Optional[MonitorSettings] = None, transport: Optional[httpx.BaseTransport] = None):
        self.s = settings or get_settings()
        if not self.s.TIKHUB_API_KEY:
            raise SourceError("未配置 TIKHUB_API_KEY")
        self._client = httpx.Client(
            base_url=self.s.TIKHUB_BASE_URL.rstrip("/"),
            headers={"Authorization": f"Bearer {self.s.TIKHUB_API_KEY}"},
            timeout=self.s.TIKHUB_TIMEOUT,
            transport=transport,
        )
        self.calls = 0  # 本次运行的成功调用次数，便于估算费用

    def close(self) -> None:
        self._client.close()

    def request(self, method: str, path: str, *, params: Optional[dict] = None, json: Any = None) -> Any:
        last_err: Optional[Exception] = None
        for attempt in range(self.s.TIKHUB_MAX_RETRIES + 1):
            try:
                resp = self._client.request(method, path, params=params, json=json)
            except httpx.HTTPError as e:
                last_err = e
            else:
                if resp.status_code == 200:
                    body = resp.json()
                    code = body.get("code") if isinstance(body, dict) else None
                    if code not in (None, 200):
                        raise SourceError(f"TikHub {path} 返回错误码 {code}: {body.get('message')}")
                    self.calls += 1
                    return body.get("data", body) if isinstance(body, dict) else body
                if resp.status_code in (429, 500, 502, 503, 504):
                    last_err = SourceError(f"TikHub {path} HTTP {resp.status_code}")
                else:
                    raise SourceError(f"TikHub {path} HTTP {resp.status_code}: {resp.text[:300]}")
            if attempt < self.s.TIKHUB_MAX_RETRIES:
                time.sleep(min(2 ** attempt, 20))
        raise SourceError(f"TikHub {path} 重试后仍失败: {last_err}")

    # -- 搜索
    def search_videos(self, keyword: str, pages: int) -> List[VideoRecord]:
        cursor, search_id, backtrace = 0, "", ""
        results: List[VideoRecord] = []
        for _ in range(max(1, pages)):
            data = self.request(
                "POST",
                SEARCH_PATH,
                json={
                    "keyword": keyword,
                    "cursor": cursor,
                    "sort_type": "2",  # 最新发布
                    "publish_time": "1",  # 最近一天
                    "filter_duration": "0",
                    "content_type": "0",  # 视频和图文都要
                    "search_id": search_id,
                    "backtrace": backtrace,
                },
            )
            results.extend(parse_awemes(data))
            has_more = find_key(data, "has_more")
            next_cursor = find_key(data, "cursor")
            if not has_more or next_cursor in (None, cursor):
                break
            cursor = int(next_cursor)
            search_id = find_key(data, "search_id") or search_id
            backtrace = find_key(data, "backtrace") or backtrace
        return results

    # -- 博主作品
    def user_posts_since(self, sec_uid: str, since_ts: int, max_pages: int = 3) -> List[VideoRecord]:
        max_cursor = 0
        results: List[VideoRecord] = []
        for _ in range(max_pages):
            data = self.request(
                "GET",
                USER_POSTS_PATH,
                params={"sec_user_id": sec_uid, "max_cursor": max_cursor, "count": 20, "sort_type": 0},
            )
            page = parse_awemes(data)
            # 置顶视频可能很旧，所以不能遇到旧视频就停，只在整页都旧时停
            fresh = [r for r in page if (r.create_time or 0) >= since_ts]
            results.extend(fresh)
            if not page or not fresh or not find_key(data, "has_more"):
                break
            max_cursor = find_key(data, "max_cursor") or 0
            if not max_cursor:
                break
        return results

    # -- 批量详情
    def multi_video(self, aweme_ids: List[str]) -> Dict[str, VideoRecord]:
        out: Dict[str, VideoRecord] = {}
        for i in range(0, len(aweme_ids), MULTI_VIDEO_BATCH):
            batch = aweme_ids[i : i + MULTI_VIDEO_BATCH]
            data = self.request("POST", MULTI_VIDEO_PATH, json=batch)
            for rec in parse_awemes(data):
                if rec.aweme_id in batch:
                    out[rec.aweme_id] = rec
        return out


class TikHubSource(DiscoverySource, TrackingSource):
    name = "tikhub"

    def __init__(self, client: Optional[TikHubClient] = None, settings: Optional[MonitorSettings] = None):
        self.s = settings or get_settings()
        self.client = client or TikHubClient(self.s)

    def search_recent(self, keywords: List[str]) -> Dict[str, List[VideoRecord]]:
        out: Dict[str, List[VideoRecord]] = {}
        for kw in keywords:
            try:
                out[kw] = self.client.search_videos(kw, self.s.STOCK_VIDEO_SEARCH_PAGES)
                logger.info(f"[TikHub] 关键词「{kw}」搜到 {len(out[kw])} 条")
            except SourceError as e:
                logger.warning(f"[TikHub] 关键词「{kw}」搜索失败：{e}")
        if keywords and not out:
            raise SourceError("TikHub 所有关键词搜索均失败")
        return out

    def creator_recent_posts(self, sec_uids: Iterable[str], since_ts: int) -> Dict[str, List[VideoRecord]]:
        out: Dict[str, List[VideoRecord]] = {}
        for uid in sec_uids:
            try:
                out[uid] = self.client.user_posts_since(uid, since_ts)
            except SourceError as e:
                logger.warning(f"[TikHub] 博主 {uid} 作品拉取失败：{e}")
        return out

    def fetch_videos(self, aweme_ids: List[str]) -> Dict[str, VideoRecord]:
        return self.client.multi_video(aweme_ids)
