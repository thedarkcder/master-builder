from __future__ import annotations

from collections.abc import Generator

from sqlalchemy.orm import Session

from orchestrator.storage.db import create_session_factory


def get_session() -> Generator[Session, None, None]:
    session_factory = create_session_factory()
    session = session_factory()
    try:
        yield session
    finally:
        session.close()
