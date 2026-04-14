from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


def database_backend_name(database_url: str) -> str | None:
    try:
        parsed = make_url(database_url)
    except ArgumentError:
        return None
    return parsed.get_backend_name()


def is_postgres_database_url(database_url: str) -> bool:
    return database_backend_name(database_url) == "postgresql"


def sqlite_allowed_for_tests(*, allow_sqlite_for_tests: bool) -> bool:
    return bool(allow_sqlite_for_tests)


def ensure_postgres_database_url(
    *,
    database_url: str,
    context: str,
    allow_sqlite_for_tests: bool = False,
) -> None:
    backend_name = database_backend_name(database_url)
    if backend_name == "postgresql":
        return
    if backend_name == "sqlite" and sqlite_allowed_for_tests(allow_sqlite_for_tests=allow_sqlite_for_tests):
        return
    raise RuntimeError(
        f"{context} requires PostgreSQL; set ORCHESTRATOR_DATABASE_URL to a postgresql URL."
    )
