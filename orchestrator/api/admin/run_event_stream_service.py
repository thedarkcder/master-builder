from __future__ import annotations

import json
from collections.abc import Iterator

from fastapi import HTTPException, status
from sqlalchemy import desc, select

from orchestrator.storage.models import AgentLifecycleEvent, RunLogEvent
from orchestrator.storage.run_event_stream import RUN_EVENT_NOTIFY_CHANNEL
from orchestrator.storage.run_queue_events import is_postgres_database_url, postgres_dsn_from_database_url


def stream_run_events_ndjson(
    *,
    session,
    run_id: str,
    run_model,
    settings,
    psycopg_module,
) -> Iterator[str]:  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")

    initial_rows = session.execute(
        select(AgentLifecycleEvent)
        .where(AgentLifecycleEvent.run_id == run_id)
        .order_by(desc(AgentLifecycleEvent.recorded_at), desc(AgentLifecycleEvent.event_id))
        .limit(200)
    ).scalars().all()
    for row in reversed(initial_rows):
        yield (
            json.dumps(
                {
                    "event_type": row.event_type,
                    "run_id": row.run_id,
                    "issue_key": row.issue_key,
                    "project_id": row.project_id,
                    "agent_id": row.agent_id,
                    "recorded_at": row.recorded_at.isoformat(),
                },
                separators=(",", ":"),
            )
            + "\n"
        )
    initial_logs = session.execute(
        select(RunLogEvent)
        .where(RunLogEvent.run_id == run_id)
        .order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id))
        .limit(500)
    ).scalars().all()
    for row in reversed(initial_logs):
        yield (
            json.dumps(
                {
                    "event_kind": "run_log",
                    "run_id": row.run_id,
                    "issue_key": row.issue_key,
                    "project_id": row.project_id,
                    "agent_id": row.agent_id,
                    "stage": row.stage,
                    "attempt": row.attempt,
                    "stream": row.stream,
                    "message": row.message,
                    "recorded_at": row.recorded_at.isoformat(),
                },
                separators=(",", ":"),
            )
            + "\n"
        )

    if psycopg_module is None or not is_postgres_database_url(settings.database_url):
        return

    with psycopg_module.connect(postgres_dsn_from_database_url(settings.database_url), autocommit=True) as conn:
        conn.execute(f'LISTEN "{RUN_EVENT_NOTIFY_CHANNEL}"')
        for notification in conn.notifies():
            try:
                payload = json.loads(str(notification.payload or "{}"))
            except json.JSONDecodeError:
                continue
            if str(payload.get("run_id") or "") != run_id:
                continue
            yield json.dumps(payload, separators=(",", ":")) + "\n"
