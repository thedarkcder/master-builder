from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

RUN_QUEUE_NOTIFY_CHANNEL = "run_queue_events"

logger = logging.getLogger(__name__)


def is_postgres_database_url(database_url: str) -> bool:
    try:
        parsed = make_url(database_url)
    except Exception:
        return False
    return parsed.get_backend_name() == "postgresql"


def postgres_dsn_from_database_url(database_url: str) -> str:
    parsed = make_url(database_url)
    if parsed.get_backend_name() != "postgresql":
        raise ValueError("LISTEN/NOTIFY worker requires a PostgreSQL database URL")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


def notify_run_enqueued(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
    issue_key: str,
) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return

    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "issue_key": issue_key,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": RUN_QUEUE_NOTIFY_CHANNEL, "payload": payload},
        )
    except Exception:
        logger.exception(
            "run_queue_notify_failed tenant_id=%s run_id=%s issue_key=%s",
            tenant_id,
            run_id,
            issue_key,
        )
