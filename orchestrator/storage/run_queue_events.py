from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import Session

RUN_QUEUE_NOTIFY_CHANNEL = "run_queue_events"

logger = logging.getLogger(__name__)


def is_postgres_database_url(database_url: str) -> bool:
    try:
        parsed = make_url(database_url)
    except ArgumentError as exc:
        logger.error("run_queue_database_url_parse_failed error=%s", exc)
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
    project_id: str | None,
    run_id: str,
    issue_key: str,
) -> None:
    _notify_worker_event(
        session,
        payload={
            "event_kind": "run_enqueued",
            "tenant_id": tenant_id,
            "project_id": project_id,
            "run_id": run_id,
            "issue_key": issue_key,
        },
        log_context={
            "tenant_id": tenant_id,
            "project_id": project_id,
            "run_id": run_id,
            "issue_key": issue_key,
            "event_kind": "run_enqueued",
        },
    )


def notify_webhook_job_enqueued(
    session: Session,
    *,
    transport: str,
    tenant_id: str | None,
    project_id: str | None,
    subject_key: str,
    job_id: str,
    dedupe_key: str | None,
) -> None:
    _notify_worker_event(
        session,
        payload={
            "event_kind": "webhook_job_enqueued",
            "transport": transport,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "subject_key": subject_key,
            "job_id": job_id,
            "dedupe_key": dedupe_key,
        },
        log_context={
            "transport": transport,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "subject_key": subject_key,
            "job_id": job_id,
            "dedupe_key": dedupe_key,
            "event_kind": "webhook_job_enqueued",
        },
    )


def _notify_worker_event(
    session: Session,
    *,
    payload: dict[str, object],
    log_context: dict[str, object],
) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return

    serialized_payload = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    try:
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": RUN_QUEUE_NOTIFY_CHANNEL, "payload": serialized_payload},
        )
    except Exception as exc:
        logger.exception(
            "worker_queue_notify_failed tenant_id=%s project_id=%s run_id=%s subject_key=%s job_id=%s dedupe_key=%s event_kind=%s error=%s",
            log_context.get("tenant_id"),
            log_context.get("project_id"),
            log_context.get("run_id"),
            log_context.get("subject_key"),
            log_context.get("job_id"),
            log_context.get("dedupe_key"),
            log_context.get("event_kind"),
            exc,
        )
