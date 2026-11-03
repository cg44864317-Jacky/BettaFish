# -*- coding: utf-8 -*-
"""
MediaCrawler 抖音关键词搜索适配

复用 DeepSentimentCrawling 里配置 MediaCrawler 数据库的逻辑，以「search」模式运行一次，
只抓最近一天发布的视频、不抓评论，然后从 MediaCrawler 写入的 douyin_aweme 表读回结果。

说明：
- MediaCrawler 的博主模式会抓博主全部历史作品，开销太大，所以博主池只走 TikHub。
- MediaCrawler 使用「非商业学习许可证」，仅限学习研究，禁止大规模抓取和商业用途。
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import text

from ..db import get_engine
from ..settings import MINDSPIDER_ROOT, MonitorSettings, get_settings
from .base import DiscoverySource, SourceError, VideoRecord

MEDIACRAWLER_PATH = MINDSPIDER_ROOT / "DeepSentimentCrawling" / "MediaCrawler"
HASHTAG_RE = re.compile(r"#([^#\s@]{1,30})")


def _to_int(v) -> Optional[int]:
    try:
        return int(str(v).replace(",", "")) if v not in (None, "", "None") else None
    except ValueError:
        return None


def _set_config_lines(path: Path, values: Dict[str, str]) -> None:
    """把 path 中以 `KEY = ` 开头的行替换为新值（只处理单行赋值）。"""
    lines = path.read_text(encoding="utf-8").split("\n")
    out, skip_paren = [], False
    for line in lines:
        if skip_paren:
            if line.strip() in (")", "]"):
                skip_paren = False
            continue
        for key, value in values.items():
            if line.startswith(f"{key} = "):
                out.append(f"{key} = {value}")
                if line.rstrip().endswith(("(", "[")):
                    skip_paren = True
                break
        else:
            out.append(line)
    path.write_text("\n".join(out), encoding="utf-8")


class MediaCrawlerSource(DiscoverySource):
    name = "mediacrawler"

    def __init__(self, settings: Optional[MonitorSettings] = None, login_type: str = "qrcode"):
        self.s = settings or get_settings()
        self.login_type = login_type
        if not (MEDIACRAWLER_PATH / "main.py").exists():
            raise SourceError("MediaCrawler 子模块未初始化，请运行 git submodule update --init --recursive")

    def _configure(self, keywords: List[str]) -> str:
        sys.path.insert(0, str(MINDSPIDER_ROOT / "DeepSentimentCrawling"))
        sys.path.insert(0, str(MINDSPIDER_ROOT))
        from platform_crawler import PlatformCrawler  # type: ignore

        if not PlatformCrawler().configure_mediacrawler_db():
            raise SourceError("MediaCrawler 数据库配置失败")
        is_pg = (self.s.DB_DIALECT or "mysql").lower() in ("postgresql", "postgres")
        save_option = "postgres" if is_pg else "db"
        _set_config_lines(
            MEDIACRAWLER_PATH / "config" / "base_config.py",
            {
                "PLATFORM": '"dy"',
                "KEYWORDS": repr(",".join(keywords)),
                "CRAWLER_TYPE": '"search"',
                "SAVE_DATA_OPTION": f'"{save_option}"',
                "CRAWLER_MAX_NOTES_COUNT": str(self.s.STOCK_VIDEO_MEDIACRAWLER_MAX_NOTES),
                "ENABLE_GET_COMMENTS": "False",
                "ENABLE_GET_SUB_COMMENTS": "False",
                "HEADLESS": "True",
            },
        )
        _set_config_lines(MEDIACRAWLER_PATH / "config" / "dy_config.py", {"PUBLISH_TIME_TYPE": "1"})  # 一天内
        return save_option

    def search_recent(self, keywords: List[str]) -> Dict[str, List[VideoRecord]]:
        if not keywords:
            return {}
        save_option = self._configure(keywords)
        start_ms = int(time.time() * 1000)
        cmd = [
            sys.executable, "main.py", "--platform", "dy", "--lt", self.login_type,
            "--type", "search", "--save_data_option", save_option,
        ]
        logger.info(f"[MediaCrawler] 搜索 {len(keywords)} 个关键词：{' '.join(cmd)}")
        try:
            result = subprocess.run(cmd, cwd=MEDIACRAWLER_PATH, timeout=3600)
        except subprocess.TimeoutExpired as e:
            raise SourceError("MediaCrawler 运行超时") from e
        if result.returncode != 0:
            raise SourceError(f"MediaCrawler 退出码 {result.returncode}（可能是登录失效或被风控）")
        out = self.read_back(keywords, start_ms)
        if not any(out.values()):
            raise SourceError("MediaCrawler 运行完成但没有抓到任何视频（可能是登录失效或被风控）")
        return out

    @staticmethod
    def read_back(keywords: List[str], since_ms: int) -> Dict[str, List[VideoRecord]]:
        sql = text(
            "SELECT aweme_id, aweme_type, title, `desc`, create_time, user_id, sec_uid, nickname, "
            "liked_count, comment_count, share_count, collected_count, video_download_url, note_download_url, "
            "cover_url, source_keyword "
            "FROM douyin_aweme WHERE add_ts >= :since OR last_modify_ts >= :since"
        )
        engine = get_engine()
        if engine.dialect.name.startswith("postgres"):
            sql = text(str(sql).replace("`desc`", '"desc"'))
        out: Dict[str, List[VideoRecord]] = {kw: [] for kw in keywords}
        with engine.connect() as conn:
            for row in conn.execute(sql, {"since": since_ms}).mappings():
                kw = row["source_keyword"] or ""
                if kw not in out:
                    continue
                desc = row["desc"] or ""
                # 图文作品的图片地址以英文逗号拼接存在 note_download_url 里
                images = [u for u in (row["note_download_url"] or "").split(",") if u.startswith("http")]
                out[kw].append(
                    VideoRecord(
                        aweme_id=str(row["aweme_id"]),
                        desc=desc,
                        title=row["title"] if row["title"] and row["title"] != desc else "",
                        hashtags=list(dict.fromkeys(HASHTAG_RE.findall(desc))),
                        create_time=_to_int(row["create_time"]),
                        author_uid=row["user_id"],
                        sec_uid=row["sec_uid"],
                        nickname=row["nickname"],
                        liked_count=_to_int(row["liked_count"]),
                        comment_count=_to_int(row["comment_count"]),
                        share_count=_to_int(row["share_count"]),
                        collected_count=_to_int(row["collected_count"]),
                        video_url=None if images else (row["video_download_url"] or None),
                        image_urls=images,
                        cover_url=row["cover_url"] or None,
                        aweme_type=_to_int(row["aweme_type"]),
                        source="mediacrawler",
                    )
                )
        return out
