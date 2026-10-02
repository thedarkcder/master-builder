from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import get_settings
from orchestrator.storage.database_support import ensure_postgres_database_url
from orchestrator.storage.runtime_role import validate_runtime_connection
from orchestrator.storage.tenant_rls import RLSSession


@lru_cache(maxsize=8)
def _engine_for_config(
    database_url: str,
    pool_size: int,
    max_overflow: int,
    pool_timeout_seconds: int,
    pool_recycle_seconds: int,
    pool_pre_ping: bool,
) -> Engine:
    backend_name = make_url(database_url).get_backend_name()
    engine_kwargs: dict[str, object] = {"future": True}
    if backend_name != "sqlite":
        engine_kwargs.update(
            {
                "pool_size": pool_size,
                "max_overflow": max_overflow,
                "pool_timeout": pool_timeout_seconds,
                "pool_recycle": pool_recycle_seconds,
                "pool_pre_ping": bool(pool_pre_ping),
            }
        )
    engine = create_engine(database_url, **engine_kwargs)
    if backend_name == "postgresql":
        event.listen(engine, "connect", validate_runtime_connection)
    return engine


def create_db_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    url = settings.database_url if database_url is None else database_url
    ensure_postgres_database_url(
        database_url=url,
        context="Runtime database",
        allow_sqlite_for_tests=settings.allow_sqlite_for_tests,
    )
    return _engine_for_config(
        url,
        int(settings.db_pool_size),
        int(settings.db_pool_max_overflow),
        int(settings.db_pool_timeout_seconds),
        int(settings.db_pool_recycle_seconds),
        bool(settings.db_pool_pre_ping),
    )


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = create_db_engine(database_url)
    return sessionmaker(
        bind=engine,
        class_=RLSSession,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def reset_db_engine_cache() -> None:
    _engine_for_config.cache_clear()
