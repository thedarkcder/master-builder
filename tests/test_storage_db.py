from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.storage.db import create_db_engine, reset_db_engine_cache


def _settings_for(database_url: str) -> SimpleNamespace:
    return SimpleNamespace(
        database_url=database_url,
        db_pool_size=11,
        db_pool_max_overflow=17,
        db_pool_timeout_seconds=45,
        db_pool_recycle_seconds=900,
        db_pool_pre_ping=True,
    )


def test_create_db_engine_applies_postgres_pool_settings() -> None:
    reset_db_engine_cache()
    with (
        patch("orchestrator.storage.db.get_settings", return_value=_settings_for("postgresql+psycopg://db/test")),
        patch("orchestrator.storage.db.create_engine", return_value=object()) as create_engine_mock,
    ):
        create_db_engine()

    create_engine_mock.assert_called_once_with(
        "postgresql+psycopg://db/test",
        future=True,
        pool_size=11,
        max_overflow=17,
        pool_timeout=45,
        pool_recycle=900,
        pool_pre_ping=True,
    )


def test_create_db_engine_skips_queuepool_overrides_for_sqlite() -> None:
    reset_db_engine_cache()
    with (
        patch("orchestrator.storage.db.get_settings", return_value=_settings_for("sqlite:///./test.db")),
        patch("orchestrator.storage.db.create_engine", return_value=object()) as create_engine_mock,
    ):
        create_db_engine()

    create_engine_mock.assert_called_once_with("sqlite:///./test.db", future=True)
