# -*- coding: utf-8 -*-
"""
TikHub 微信视频号接口适配（接口和字段均已用真实请求验证，2026-11）

- POST /api/v1/wechat_search/v2/fetch_search_videos   关键词搜视频号作品
      keyword；sort=latest（按发布时间倒序）；publish_time=day（一天内）；分页用上一页返回的 offset + cursor
      每页 10 条；结果只有标题、作者昵称、发布时间、封面，没有互动数
- POST /api/v1/wechat_channels/v2/fetch_video_detail   作品详情（含互动数、作者 username）
      export_id / object_id / share_url 三选一
- POST /api/v1/wechat_channels/v2/fetch_user_videos    作者主页作品列表（每页约 15 条，含互动数）
      username（…@finder）；分页用 last_buffer

作品的稳定标识：搜索结果 jumpInfo.extInfo.feedNonceId = 详情/列表里 objectNonceId 的第一段。
exportId 每次请求都会变化，不能用来对应同一条作品。

互动数对应关系（视频号没有公开「收藏数」）：
    favCount → 点赞（liked_count）    likeCount → 推荐/喜欢（存放在 collected_count 列）
    forwardCount → 转发（share_count） commentCount → 评论（comment_count）

视频号的视频文件是加密的（需要 decodeKey 解密），所以不下载视频；
识别时使用标题文案 + 封面（图文作品为全部图片）的文字识别。
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional

from loguru import logger
from sqlalchemy import select

from ..settings import MonitorSettings, get_settings
from .base import DiscoverySource, SourceError, TrackingSource, VideoRecord
from .tikhub import TikHubClient, find_key

SEARCH_PATH = "/api/v1/wechat_search/v2/fetch_search_videos"
DETAIL_PATH = "/api/v1/wechat_channels/v2/fetch_video_detail"
USER_VIDEOS_PATH = "/api/v1/wechat_channels/v2/fetch_user_videos"
ID_PREFIX = "wx_"
WECHAT_IMAGE_TYPE = 68  # 视频号图文作品统一记为抖音的图文类型，报表显示为「图文」

TAG_RE = re.compile(r"<[^>]+>")
HASHTAG_RE = re.compile(r"#([^#\s@<]{1,30})")


def _int(v: Any) -> Optional[int]:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _clean(text: Optional[str]) -> str:
    return TAG_RE.sub("", text or "").replace("&nbsp;", " ").strip()


def _duration_ms(text: Optional[str]) -> Optional[int]:
    if not text or ":" not in str(text):
        return None
    try:
        parts = [int(x) for x in str(text).split(":")]
    except ValueError:
        return None
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec * 1000


def _play_len_ms(media: List[Dict[str, Any]], is_image: bool) -> Optional[int]:
    sec = _int(media[0].get("videoPlayLen")) if media and not is_image else None
    return sec * 1000 if sec else None


def video_key(nonce: Optional[str]) -> Optional[str]:
    nonce = str(nonce or "").split("_")[0]
    return ID_PREFIX + nonce if nonce.isdigit() else None


def parse_search_item(it: Dict[str, Any]) -> Optional[VideoRecord]:
    try:
        ext = json.loads((it.get("jumpInfo") or {}).get("extInfo") or "{}")
    except ValueError:
        ext = {}
    key = video_key(ext.get("feedNonceId"))
    if not key:
        return None
    desc = _clean(it.get("title"))
    return VideoRecord(
        aweme_id=key, desc=desc, hashtags=list(dict.fromkeys(HASHTAG_RE.findall(desc))),
        create_time=_int(it.get("pubTime")), nickname=(it.get("source") or {}).get("title"),
        cover_url=it.get("image") or None, duration_ms=_duration_ms(it.get("duration")),
        export_id=it.get("exportId") or ext.get("encryptedObjectId"), aweme_type=0,
        source="tikhub_wechat", platform="wechat",
    )


def _media_url(m: Dict[str, Any], url_key: str = "url", token_key: str = "urlToken") -> Optional[str]:
    url = m.get(url_key)
    return (url + (m.get(token_key) or "")) if url else None


def parse_object(o: Dict[str, Any]) -> Optional[VideoRecord]:
    """作品详情 / 作者作品列表里的 object 结构。"""
    key = video_key(o.get("objectNonceId"))
    if not key:
        return None
    desc_obj = o.get("objectDesc") or {}
    media = [m for m in (desc_obj.get("media") or []) if isinstance(m, dict)]
    contact = o.get("contact") or {}
    is_image = _int(desc_obj.get("mediaType")) == 2
    cover = None
    if media:
        cover = (_media_url(media[0], "coverUrl", "coverUrlToken") or _media_url(media[0], "thumbUrl", "thumbUrlToken")
                 or (_media_url(media[0]) if is_image else None))
    desc = (desc_obj.get("description") or "").strip()
    username = o.get("username") or contact.get("username")
    return VideoRecord(
        aweme_id=key, desc=desc, hashtags=list(dict.fromkeys(HASHTAG_RE.findall(desc))),
        create_time=_int(o.get("createtime")), author_uid=username, sec_uid=username,
        nickname=o.get("nickname") or contact.get("nickname"),
        follower_count=_int(contact.get("fansCount")) or None,
        liked_count=_int(o.get("favCount")), collected_count=_int(o.get("likeCount")),
        share_count=_int(o.get("forwardCount")), comment_count=_int(o.get("commentCount")),
        cover_url=cover, duration_ms=_play_len_ms(media, is_image),
        aweme_type=WECHAT_IMAGE_TYPE if is_image else 0,
        image_urls=[u for u in (_media_url(m) for m in media) if u] if is_image else [],
        object_id=str(o["id"]) if o.get("id") else None, export_id=o.get("exportId"),
        source="tikhub_wechat", platform="wechat",
    )


def iter_objects(payload: Any) -> Iterable[Dict[str, Any]]:
    """找出响应里所有作品 object（含 objectNonceId 与 objectDesc 的字典）。"""
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("objectNonceId") and "objectDesc" in node:
                yield node
                continue
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(reversed(node))


def parse_objects(payload: Any) -> List[VideoRecord]:
    seen, out = set(), []
    for o in iter_objects(payload):
        rec = parse_object(o)
        if rec and rec.aweme_id not in seen:
            seen.add(rec.aweme_id)
            out.append(rec)
    return out


def search_items(data: Any) -> List[Dict[str, Any]]:
    try:
        boxes = data["results"]["data"]
    except (KeyError, TypeError):
        return []
    return [it for box in boxes or [] for sb in (box.get("subBoxes") or []) for it in (sb.get("items") or [])]


class WechatChannelsSource(DiscoverySource, TrackingSource):
    name = "tikhub_wechat"
    platform = "wechat"
    refresh_before_judge = False  # 粗筛通过的作品不必先查详情（每条单独计费），标记后再查
    fetch_on_flag = True

    def __init__(self, client: Optional[TikHubClient] = None, settings: Optional[MonitorSettings] = None):
        self.s = settings or get_settings()
        self.client = client or TikHubClient(self.s)

    # ---------------------------------------------------------------- 搜索
    def search_keyword(self, keyword: str, pages: int, publish_days: int = 1) -> List[VideoRecord]:
        offset, cursor = 0, None
        results: Dict[str, VideoRecord] = {}
        for page in range(max(1, pages)):
            body = {"keyword": keyword, "sort": "latest", "offset": offset, "cursor": cursor,
                    "publish_time": "day" if publish_days <= 1 else "all"}
            try:
                data = self.client.request("POST", SEARCH_PATH, json=body)
            except SourceError as e:
                if page == 0:
                    raise
                logger.warning(f"[视频号] 「{keyword}」第 {page + 1} 页失败，保留前 {page} 页：{e}")
                break
            items = search_items(data)
            for rec in filter(None, (parse_search_item(it) for it in items)):
                results.setdefault(rec.aweme_id, rec)
            if not items or not (data or {}).get("continue_flag", 1) or not (data or {}).get("cursor"):
                break
            offset, cursor = data.get("offset") or offset + len(items), data.get("cursor")
        return list(results.values())

    def search_recent(self, keywords: List[str], publish_days: int = 1) -> Dict[str, List[VideoRecord]]:
        out: Dict[str, List[VideoRecord]] = {}
        pages = self.s.STOCK_VIDEO_WECHAT_SEARCH_PAGES
        for kw in keywords:
            try:
                out[kw] = self.search_keyword(kw, pages, publish_days)
                logger.info(f"[视频号] 关键词「{kw}」搜到 {len(out[kw])} 条")
            except SourceError as e:
                logger.warning(f"[视频号] 关键词「{kw}」搜索失败：{e}")
        if keywords and not out:
            raise SourceError("视频号所有关键词搜索均失败")
        return out

    # ---------------------------------------------------------------- 作者作品
    def user_videos_since(self, username: str, since_ts: int, max_pages: int = 3) -> List[VideoRecord]:
        buf, out = "", []
        for _ in range(max_pages):
            data = self.client.request("POST", USER_VIDEOS_PATH, json={"username": username, "last_buffer": buf})
            page = parse_objects(data)
            out.extend(page)
            buf = find_key(data, "lastBuffer") or find_key(data, "last_buffer") or ""
            if not page or not buf or min((r.create_time or 0) for r in page) < since_ts:
                break
        return out

    def creator_recent_posts(self, sec_uids: Iterable[str], since_ts: int) -> Dict[str, List[VideoRecord]]:
        out: Dict[str, List[VideoRecord]] = {}
        for uid in sec_uids:
            try:
                out[uid] = [r for r in self.user_videos_since(uid, since_ts, max_pages=1)
                            if (r.create_time or 0) >= since_ts]
            except SourceError as e:
                logger.warning(f"[视频号] 博主 {uid[:20]}… 作品拉取失败：{e}")
        return out

    # ---------------------------------------------------------------- 详情
    def detail(self, export_id: Optional[str] = None, object_id: Optional[str] = None) -> Optional[VideoRecord]:
        for body in ({"export_id": export_id} if export_id else None, {"object_id": object_id} if object_id else None):
            if not body:
                continue
            try:
                recs = parse_objects(self.client.request("POST", DETAIL_PATH, json=body))
            except SourceError as e:
                logger.debug(f"[视频号] 详情查询失败 {list(body)[0]}：{e}")
                continue
            if recs:
                return recs[0]
        return None

    def fetch_videos(self, aweme_ids: List[str]) -> Dict[str, VideoRecord]:
        """按作者批量查（一次请求覆盖该作者约 15 条作品），查不到的再逐条查详情。"""
        from ..db import session_scope
        from ..models import DouyinStockVideo

        with session_scope() as s:
            rows = {r.aweme_id: (r.sec_uid, r.create_time, r.export_id, r.object_id) for r in s.scalars(
                select(DouyinStockVideo).where(DouyinStockVideo.aweme_id.in_(aweme_ids)))}
        found: Dict[str, VideoRecord] = {}
        by_author: Dict[str, List[str]] = {}
        for aid, (uname, _, _, _) in rows.items():
            if uname:
                by_author.setdefault(uname, []).append(aid)
        for uname, ids in by_author.items():
            oldest = min((rows[a][1] or 0) for a in ids)
            try:
                for rec in self.user_videos_since(uname, oldest):
                    if rec.aweme_id in rows:
                        found[rec.aweme_id] = rec
            except SourceError as e:
                logger.warning(f"[视频号] 作者 {uname[:20]}… 作品列表获取失败，改为逐条查询：{e}")
        for aid in aweme_ids:
            if aid in found or aid not in rows:
                continue
            rec = self.detail(rows[aid][2], rows[aid][3])
            if rec and rec.aweme_id == aid:
                found[aid] = rec
        return found
