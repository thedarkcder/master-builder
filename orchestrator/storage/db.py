from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import get_settings


@lru_cache(maxsize=8)
def _engine_for_url(database_url: str) -> Engine:
    return create_engine(database_url, future=True)


def create_db_engine(database_url: str | None = None) -> Engine:
    url = database_url or get_settings().database_url
    return _engine_for_url(url)


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = create_db_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def reset_db_engine_cache() -> None:
    _engine_for_url.cache_clear()
