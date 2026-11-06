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


# MediaCrawler 的 ORM 把这些列定义为 BigInteger，MindSpider 建表时却是 VARCHAR；
# PostgreSQL 不会隐式比较 varchar 与 bigint，导致 MediaCrawler 入库时报
# 「操作符不存在: character varying = bigint」。MySQL 会隐式转换，不受影响。
MEDIACRAWLER_BIGINT_COLUMNS = {
    "douyin_aweme": ["aweme_id"],
    "douyin_aweme_comment": ["aweme_id", "comment_id"],
}


def align_mediacrawler_schema(engine=None) -> List[str]:
    """PostgreSQL 下把上述列从 varchar 改为 bigint（只在类型不一致时执行），返回改动的列。"""
    from sqlalchemy import inspect

    engine = engine or get_engine()
    if not engine.dialect.name.startswith("postgres"):
        return []
    insp, changed = inspect(engine), []
    tables = set(insp.get_table_names())
    for table, cols in MEDIACRAWLER_BIGINT_COLUMNS.items():
        if table not in tables:
            continue
        types = {c["name"]: str(c["type"]).upper() for c in insp.get_columns(table)}
        for col in cols:
            if col in types and ("CHAR" in types[col] or "TEXT" in types[col]):
                with engine.begin() as conn:
                    conn.execute(text(
                        f'ALTER TABLE {table} ALTER COLUMN {col} TYPE BIGINT '
                        f"USING NULLIF(regexp_replace({col}, '[^0-9]', '', 'g'), '')::bigint"
                    ))
                changed.append(f"{table}.{col}")
    if changed:
        logger.info(f"[MediaCrawler] 已把以下列改为 BIGINT 以兼容 MediaCrawler：{', '.join(changed)}")
    return changed


class MediaCrawlerSource(DiscoverySource):
    name = "mediacrawler"

    def __init__(self, settings: Optional[MonitorSettings] = None, login_type: str = "qrcode",
                 max_notes: Optional[int] = None):
        self.s = settings or get_settings()
        self.login_type = login_type
        self.max_notes = max_notes
        if not (MEDIACRAWLER_PATH / "main.py").exists():
            raise SourceError("MediaCrawler 子模块未初始化，请运行 git submodule update --init --recursive")

    def _configure(self, keywords: List[str], publish_days: int = 1) -> str:
        sys.path.insert(0, str(MINDSPIDER_ROOT / "DeepSentimentCrawling"))
        sys.path.insert(0, str(MINDSPIDER_ROOT))
        from platform_crawler import PlatformCrawler  # type: ignore

        if not PlatformCrawler().configure_mediacrawler_db():
            raise SourceError("MediaCrawler 数据库配置失败")
        align_mediacrawler_schema()
        is_pg = (self.s.DB_DIALECT or "mysql").lower() in ("postgresql", "postgres")
        save_option = "postgres" if is_pg else "db"
        _set_config_lines(
            MEDIACRAWLER_PATH / "config" / "base_config.py",
            {
                "PLATFORM": '"dy"',
                "KEYWORDS": repr(",".join(keywords)),
                "CRAWLER_TYPE": '"search"',
                "SAVE_DATA_OPTION": f'"{save_option}"',
                "CRAWLER_MAX_NOTES_COUNT": str(self.max_notes or self.s.STOCK_VIDEO_MEDIACRAWLER_MAX_NOTES),
                "ENABLE_GET_COMMENTS": "False",
                "ENABLE_GET_SUB_COMMENTS": "False",
                # 必须用可见浏览器：抖音在无头浏览器里不显示登录按钮，扫码登录和滑块验证也需要看得到窗口
                "HEADLESS": "False",
                # 不用 CDP 模式：CDP 模式下页面宽度跟随屏幕，在缩放较大的屏幕上抖音的登录按钮会被挤到视口外无法点击；
                # 内置浏览器的页面固定为 1920x1080，登录状态同样会保存
                "ENABLE_CDP_MODE": "False",
            },
        )
        _set_config_lines(MEDIACRAWLER_PATH / "config" / "dy_config.py",
                          {"PUBLISH_TIME_TYPE": "7" if publish_days > 1 else "1"})  # 一天内；漏跑补抓时一周内
        return save_option

    def search_recent(self, keywords: List[str], publish_days: int = 1) -> Dict[str, List[VideoRecord]]:
        if not keywords:
            return {}
        timeout = 3600
        if self.deadline:  # 给后面的识别留出至少 30 分钟
            timeout = int(min(timeout, self.deadline - time.time() - 30 * 60))
            if timeout < 300:
                raise SourceError("距每日截止时间太近，跳过 MediaCrawler")
        save_option = self._configure(keywords, publish_days)
        start_ms = int(time.time() * 1000)
        cmd = [
            sys.executable, "main.py", "--platform", "dy", "--lt", self.login_type,
            "--type", "search", "--save_data_option", save_option, "--headless", "false",
        ]
        logger.info(f"[MediaCrawler] 搜索 {len(keywords)} 个关键词：{' '.join(cmd)}")
        attempts = max(1, self.s.STOCK_VIDEO_MEDIACRAWLER_ATTEMPTS)
        give_up_at = time.time() + timeout
        for attempt in range(1, attempts + 1):
            remaining = int(give_up_at - time.time())
            if remaining < 120:
                raise SourceError("MediaCrawler 重试时间不足（每日截止时间临近）")
            try:
                result = subprocess.run(cmd, cwd=MEDIACRAWLER_PATH, timeout=remaining)
            except subprocess.TimeoutExpired as e:
                raise SourceError("MediaCrawler 运行超时") from e
            if result.returncode == 0:
                break
            if attempt < attempts:  # 偶发的页面加载超时等，重试一次通常就能成功
                logger.warning(f"[MediaCrawler] 第 {attempt} 次运行失败（退出码 {result.returncode}），30 秒后重试")
                time.sleep(30)
        else:
            raise SourceError(f"MediaCrawler 退出码 {result.returncode}（可能是登录失效、网络慢或被风控）")
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
