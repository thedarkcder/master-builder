from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.config import get_settings


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
                "pool_size": max(1, int(pool_size)),
                "max_overflow": max(0, int(max_overflow)),
                "pool_timeout": max(1, int(pool_timeout_seconds)),
                "pool_recycle": max(1, int(pool_recycle_seconds)),
                "pool_pre_ping": bool(pool_pre_ping),
            }
        )
    return create_engine(database_url, **engine_kwargs)


def create_db_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    url = database_url or settings.database_url
    return _engine_for_config(
        url,
        int(getattr(settings, "db_pool_size", 10)),
        int(getattr(settings, "db_pool_max_overflow", 20)),
        int(getattr(settings, "db_pool_timeout_seconds", 60)),
        int(getattr(settings, "db_pool_recycle_seconds", 1800)),
        bool(getattr(settings, "db_pool_pre_ping", True)),
    )


def create_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = create_db_engine(database_url)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def reset_db_engine_cache() -> None:
    _engine_for_config.cache_clear()
