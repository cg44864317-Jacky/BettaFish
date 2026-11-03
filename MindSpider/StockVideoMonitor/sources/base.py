# -*- coding: utf-8 -*-
"""数据源统一的数据结构与接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

IMAGE_AWEME_TYPES = {2, 68, 150}  # 抖音图文作品类型


@dataclass
class VideoRecord:
    aweme_id: str
    desc: str = ""
    title: str = ""
    hashtags: List[str] = field(default_factory=list)
    create_time: Optional[int] = None  # 秒级时间戳
    author_uid: Optional[str] = None
    sec_uid: Optional[str] = None
    nickname: Optional[str] = None
    follower_count: Optional[int] = None
    liked_count: Optional[int] = None
    comment_count: Optional[int] = None
    share_count: Optional[int] = None
    collected_count: Optional[int] = None
    video_url: Optional[str] = None
    cover_url: Optional[str] = None
    duration_ms: Optional[int] = None
    aweme_type: Optional[int] = None  # 0 视频，68 图文等
    image_urls: List[str] = field(default_factory=list)  # 图文作品的图片地址
    source: str = ""
    channels: List[str] = field(default_factory=list)  # 被哪些召回路径找到

    @property
    def is_image_post(self) -> bool:
        return self.aweme_type in IMAGE_AWEME_TYPES or bool(self.image_urls)

    @property
    def aweme_url(self) -> str:
        kind = "note" if self.is_image_post else "video"
        return f"https://www.douyin.com/{kind}/{self.aweme_id}"

    @property
    def text(self) -> str:
        parts = [self.title, self.desc, " ".join(f"#{t}" for t in self.hashtags)]
        return "\n".join(p for p in parts if p)


class SourceError(RuntimeError):
    pass


class DiscoverySource(ABC):
    """发现新视频（关键词搜索 / 博主主页）。"""

    name: str = "base"

    @abstractmethod
    def search_recent(self, keywords: List[str]) -> Dict[str, List[VideoRecord]]:
        """按关键词搜索最近一天发布的视频，返回 {keyword: [VideoRecord]}。"""

    def creator_recent_posts(self, sec_uids: Iterable[str], since_ts: int) -> Dict[str, List[VideoRecord]]:
        raise SourceError(f"{self.name} 不支持拉取博主作品")


class TrackingSource(ABC):
    """批量查询视频最新互动数据。"""

    name: str = "base"

    @abstractmethod
    def fetch_videos(self, aweme_ids: List[str]) -> Dict[str, VideoRecord]:
        """返回查到的视频；查不到（删除/私密/仅部分可见）的不出现在结果里。"""
