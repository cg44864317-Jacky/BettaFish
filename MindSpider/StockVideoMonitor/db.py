# -*- coding: utf-8 -*-
"""数据库连接（同步 SQLAlchemy），与 MindSpider 共用同一个库。"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional
from urllib.parse import quote_plus

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .settings import MonitorSettings, get_settings

_engine: Optional[Engine] = None
_SessionFactory: Optional[sessionmaker] = None


def build_database_url(s: MonitorSettings) -> str:
    if s.DATABASE_URL:
        return s.DATABASE_URL
    dialect = (s.DB_DIALECT or "mysql").lower()
    pwd = quote_plus(s.DB_PASSWORD or "")
    if dialect in ("postgresql", "postgres"):
        return f"postgresql+psycopg://{s.DB_USER}:{pwd}@{s.DB_HOST}:{s.DB_PORT}/{s.DB_NAME}"
    return f"mysql+pymysql://{s.DB_USER}:{pwd}@{s.DB_HOST}:{s.DB_PORT}/{s.DB_NAME}?charset={s.DB_CHARSET}"


def get_engine() -> Engine:
    global _engine, _SessionFactory
    if _engine is None:
        _engine = create_engine(build_database_url(get_settings()), future=True, pool_pre_ping=True, pool_recycle=1800)
        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def reset_engine(engine: Optional[Engine] = None) -> None:
    """测试时注入引擎。"""
    global _engine, _SessionFactory
    _engine = engine
    _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False, future=True) if engine else None


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    session: Session = _SessionFactory()  # type: ignore[misc]
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# 已有表新增的列（create_all 不会给已存在的表加列），按需补上
_ADDED_COLUMNS = {
    "a_share_company": {"ambiguous_terms": "TEXT"},
    "douyin_stock_video": {"platform": "VARCHAR(16) DEFAULT 'douyin'", "object_id": "VARCHAR(64)", "export_id": "TEXT"},
    "douyin_finance_creator": {"platform": "VARCHAR(16) DEFAULT 'douyin'"},
    "stock_video_alert": {"platform": "VARCHAR(16) DEFAULT 'douyin'"},
}


# 已有表需要加长的列（视频号作者 username 约 94 个字符，原先按抖音 uid 设的 64 不够）
_WIDENED_COLUMNS = {
    "douyin_stock_video": {"author_uid": 255, "sec_uid": 255},
    "douyin_finance_creator": {"author_uid": 255, "sec_uid": 255},
}


def _widen_columns(engine: Engine) -> None:
    from sqlalchemy import inspect, text

    dialect = engine.dialect.name
    if dialect not in ("postgresql", "mysql", "mariadb"):
        return  # SQLite 不限制 VARCHAR 长度
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    for table, cols in _WIDENED_COLUMNS.items():
        if table not in tables:
            continue
        current = {c["name"]: getattr(c["type"], "length", None) for c in insp.get_columns(table)}
        for col, length in cols.items():
            if current.get(col) is not None and current[col] < length:
                ddl = (f"ALTER TABLE {table} ALTER COLUMN {col} TYPE VARCHAR({length})" if dialect == "postgresql"
                       else f"ALTER TABLE {table} MODIFY {col} VARCHAR({length})")
                with engine.begin() as conn:
                    conn.execute(text(ddl))
    # 因列太短而保存失败的视频号作品，重新放回待识别（下次运行自动重试）
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE douyin_stock_video SET stage = 'finance', error_message = NULL "
            "WHERE stage = 'failed' AND error_message LIKE '%value too long%'"
        ))


def create_tables(engine: Optional[Engine] = None) -> None:
    from sqlalchemy import inspect, text

    from .models import MonitorBase

    engine = engine or get_engine()
    MonitorBase.metadata.create_all(engine)
    insp = inspect(engine)
    for table, cols in _ADDED_COLUMNS.items():
        existing = {c["name"] for c in insp.get_columns(table)}
        for col, ddl in cols.items():
            if col not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
    _widen_columns(engine)
