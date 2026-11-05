# -*- coding: utf-8 -*-
"""
抖音 A 股上市公司分析视频监控 —— 配置

所有配置项都可以写在项目根目录的 .env 中（与 BettaFish / MindSpider 共用同一个 .env），
也可以用同名环境变量覆盖。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

MINDSPIDER_ROOT: Path = Path(__file__).resolve().parents[1]
PROJECT_ROOT: Path = MINDSPIDER_ROOT.parent
_CWD_ENV = Path.cwd() / ".env"
ENV_FILE: str = str(_CWD_ENV if _CWD_ENV.exists() else (PROJECT_ROOT / ".env"))


class MonitorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_prefix="", case_sensitive=False, extra="ignore")

    # ------------------------------------------------------------------ 数据库
    # 与 MindSpider 共用；DATABASE_URL 优先（测试时可设为 sqlite:///xxx.db）
    DATABASE_URL: Optional[str] = Field(None, description="完整数据库连接串，设置后忽略 DB_* 配置")
    DB_DIALECT: str = Field("mysql", description="mysql 或 postgresql")
    DB_HOST: str = Field("localhost")
    DB_PORT: int = Field(3306)
    DB_USER: str = Field("root")
    DB_PASSWORD: str = Field("")
    DB_NAME: str = Field("mindspider")
    DB_CHARSET: str = Field("utf8mb4")

    # ------------------------------------------------------------------ 大模型
    # 默认复用 MindSpider 的模型配置，也可单独指定
    MINDSPIDER_API_KEY: Optional[str] = None
    MINDSPIDER_BASE_URL: Optional[str] = "https://api.deepseek.com"
    MINDSPIDER_MODEL_NAME: Optional[str] = "deepseek-chat"
    STOCK_VIDEO_LLM_API_KEY: Optional[str] = Field(None, description="不填则使用 MINDSPIDER_API_KEY")
    STOCK_VIDEO_LLM_BASE_URL: Optional[str] = Field(None, description="不填则使用 MINDSPIDER_BASE_URL")
    STOCK_VIDEO_LLM_MODEL: Optional[str] = Field(None, description="不填则使用 MINDSPIDER_MODEL_NAME")

    # ------------------------------------------------------------------ TikHub
    TIKHUB_API_KEY: Optional[str] = Field(None, description="TikHub API Token")
    TIKHUB_BASE_URL: str = Field("https://api.tikhub.io", description="TikHub 接口地址")
    TIKHUB_TIMEOUT: int = Field(60, description="单次请求超时（秒）")
    TIKHUB_MAX_RETRIES: int = Field(3, description="429/5xx 或 TikHub 提示可重试时的最大重试次数")
    STOCK_VIDEO_MEDIACRAWLER_ATTEMPTS: int = Field(2, description="MediaCrawler 失败时重试几次再改用 TikHub")

    # ------------------------------------------------------------------ 发现新视频
    STOCK_VIDEO_DISCOVERY_SOURCE: str = Field(
        "mediacrawler", description="关键词搜索用哪个数据源：mediacrawler 或 tikhub；主数据源失败时自动改用另一个"
    )
    STOCK_VIDEO_SEARCH_KEYWORDS: List[str] = Field(
        default_factory=lambda: [
            "股票", "A股", "个股分析", "涨停", "龙头股", "概念股", "财报解读", "业绩预告",
            "主力资金", "板块轮动", "上市公司", "价值投资", "炒股", "股市",
        ],
        description="通用财经搜索词（JSON 数组）",
    )
    STOCK_VIDEO_SEARCH_PAGES: int = Field(3, description="每个关键词翻几页（TikHub 每页约 10-20 条）")
    STOCK_VIDEO_MEDIACRAWLER_MAX_NOTES: int = Field(60, description="MediaCrawler 每个关键词最多抓多少条")
    STOCK_VIDEO_HOT_STOCK_KEYWORDS: int = Field(
        20, description="每天从东方财富人气榜取前 N 只股票名称作为动态搜索词，0 表示关闭"
    )
    STOCK_VIDEO_CREATOR_POOL_ENABLED: bool = Field(True, description="是否拉取财经博主池里博主的最新作品")
    STOCK_VIDEO_CREATOR_POOL_MAX: int = Field(300, description="每天最多拉取多少个博主（按最近活跃排序）")
    STOCK_VIDEO_LOOKBACK_HOURS: int = Field(26, description="只保留多少小时内发布的视频（略大于 24 防漏）")

    # ------------------------------------------------------------------ 识别
    STOCK_VIDEO_PREFILTER_BATCH: int = Field(20, description="粗筛时每次让大模型判断多少条视频")
    STOCK_VIDEO_MIN_CONFIDENCE: float = Field(0.6, description="精判置信度低于此值不标记为重点跟踪")
    STOCK_VIDEO_MEDIA_DIR: str = Field(
        str(MINDSPIDER_ROOT / "StockVideoMonitor" / "_media_cache"), description="视频临时下载目录"
    )
    STOCK_VIDEO_KEEP_MEDIA: bool = Field(False, description="解析完是否保留视频文件")
    STOCK_VIDEO_FRAME_INTERVAL: int = Field(5, description="每隔多少秒抽一帧做文字识别")
    STOCK_VIDEO_MAX_FRAMES: int = Field(10, description="每条视频最多抽多少帧")
    STOCK_VIDEO_OCR_TIMEOUT: int = Field(180, description="单条作品文字识别的最长时间（秒），超时跳过")
    STOCK_VIDEO_DOWNLOAD_TIMEOUT: int = Field(180, description="单个视频/图片下载的最长时间（秒）")
    STOCK_VIDEO_MAX_DOWNLOAD_MB: int = Field(300, description="单个视频最大下载体积（MB）")
    STOCK_VIDEO_ANALYSIS_BUDGET_MINUTES: int = Field(
        120, description="每次运行解析视频的总时长上限（分钟），超过后剩余作品只用文字判断，保证任务按时结束"
    )

    # 语音识别：none | command | openai | funasr
    STOCK_VIDEO_ASR_BACKEND: str = Field("none", description="语音识别后端")
    STOCK_VIDEO_ASR_COMMAND: Optional[str] = Field(
        None, description="command 后端的命令模板，{input} 会替换为音频路径，程序读取标准输出作为识别结果"
    )
    STOCK_VIDEO_ASR_API_KEY: Optional[str] = None
    STOCK_VIDEO_ASR_BASE_URL: Optional[str] = None
    STOCK_VIDEO_ASR_MODEL: str = "whisper-1"
    # 画面文字识别：none | command | rapidocr | paddleocr
    STOCK_VIDEO_OCR_BACKEND: str = Field("none", description="画面文字识别后端")
    STOCK_VIDEO_OCR_COMMAND: Optional[str] = None

    # ------------------------------------------------------------------ 跟踪
    STOCK_VIDEO_TRACK_DAYS: int = Field(14, description="重点视频跟踪天数")
    STOCK_VIDEO_UNAVAILABLE_AFTER_MISSES: int = Field(2, description="连续几次查不到就判定视频已删除/隐藏")

    # ------------------------------------------------------------------ 预警
    STOCK_VIDEO_ALERT_SPIKE_RATIO: float = Field(3.0, description="单日增量 ≥ 自身此前日均增量的多少倍算异常")
    STOCK_VIDEO_ALERT_MIN_LIKES_DELTA: int = Field(2000, description="单日点赞增量至少多少才考虑预警（过滤小数波动）")
    STOCK_VIDEO_ALERT_MIN_SHARES_DELTA: int = Field(300)
    STOCK_VIDEO_ALERT_MIN_COLLECTS_DELTA: int = Field(500)
    STOCK_VIDEO_ALERT_FIRST_DAY_LIKES: int = Field(20000, description="新视频首日点赞超过此值直接预警")
    STOCK_VIDEO_ALERT_COMPANY_VIDEOS: int = Field(5, description="单家公司当日新增分析视频至少多少条才考虑预警")
    STOCK_VIDEO_ALERT_COMPANY_RATIO: float = Field(3.0, description="公司当日指标 ≥ 过去 7 日均值的多少倍算异常")
    STOCK_VIDEO_ALERT_COMPANY_MIN_INTERACTION: int = Field(
        10000, description="单家公司当日互动增量（点赞+转发+收藏）至少多少才考虑预警"
    )
    STOCK_VIDEO_ALERT_STANCE_SHIFT: float = Field(
        0.4, description="看空占比较过去 7 日变化超过多少（0-1）算观点反转"
    )
    STOCK_VIDEO_NOTIFIERS: List[str] = Field(default_factory=lambda: ["log"], description="通知渠道，目前仅 log")


_settings: Optional[MonitorSettings] = None


def get_settings() -> MonitorSettings:
    global _settings
    if _settings is None:
        _settings = MonitorSettings()
    return _settings


def override_settings(settings: MonitorSettings) -> None:
    """测试或脚本中替换全局配置。"""
    global _settings
    _settings = settings
