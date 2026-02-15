from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

RUN_EVENT_NOTIFY_CHANNEL = "run_event_stream"

logger = logging.getLogger(__name__)


def notify_run_event(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
    event_type: str,
    issue_key: str | None,
    project_id: str | None,
    agent_id: str | None,
    recorded_at: str,
) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "run_id": run_id,
            "event_type": event_type,
            "issue_key": issue_key,
            "project_id": project_id,
            "agent_id": agent_id,
            "recorded_at": recorded_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": RUN_EVENT_NOTIFY_CHANNEL, "payload": payload},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "run_event_notify_failed tenant_id=%s run_id=%s event_type=%s error=%s",
            tenant_id,
            run_id,
            event_type,
            exc,
        )


def notify_run_log_event(
    session: Session,
    *,
    tenant_id: str,
    run_id: str,
    issue_key: str | None,
    project_id: str | None,
    agent_id: str | None,
    stage: str,
    attempt: int | None,
    stream: str,
    message: str,
    recorded_at: str,
) -> None:
    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    payload = json.dumps(
        {
            "event_kind": "run_log",
            "tenant_id": tenant_id,
            "run_id": run_id,
            "issue_key": issue_key,
            "project_id": project_id,
            "agent_id": agent_id,
            "stage": stage,
            "attempt": attempt,
            "stream": stream,
            "message": message,
            "recorded_at": recorded_at,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        session.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {"channel": RUN_EVENT_NOTIFY_CHANNEL, "payload": payload},
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "run_log_notify_failed tenant_id=%s run_id=%s stage=%s error=%s",
            tenant_id,
            run_id,
            stage,
            exc,
        )
