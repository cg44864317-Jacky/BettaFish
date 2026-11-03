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


def create_tables(engine: Optional[Engine] = None) -> None:
    from .models import MonitorBase

    MonitorBase.metadata.create_all(engine or get_engine())
