from __future__ import annotations

import json
from collections.abc import Iterator

from sqlalchemy import desc, select

from orchestrator.storage.models import RunLogEvent
from orchestrator.storage.run_event_stream import RUN_EVENT_NOTIFY_CHANNEL
from orchestrator.storage.run_queue_events import is_postgres_database_url, postgres_dsn_from_database_url


def list_codex_log_events(
    *,
    session,
    run_log_schema_cls,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    channel: str | None = None,
    command: str | None = None,
    limit: int = 500,
):  # noqa: ANN001
    query = select(RunLogEvent).order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id))
    if tenant_id:
        query = query.where(RunLogEvent.tenant_id == tenant_id)
    if project_id:
        query = query.where(RunLogEvent.project_id == project_id)
    if run_id:
        query = query.where(RunLogEvent.run_id == run_id)
    if channel:
        query = query.where(RunLogEvent.channel == channel)
    if command:
        query = query.where(RunLogEvent.command == command)
    rows = session.execute(query.limit(max(1, min(limit, 2000)))).scalars().all()
    return [
        run_log_schema_cls(
            run_id=row.run_id,
            issue_key=row.issue_key,
            project_id=row.project_id,
            agent_id=row.agent_id,
            invocation_id=row.invocation_id,
            channel=row.channel,
            command=row.command,
            working_dir=row.working_dir,
            stage=row.stage,
            attempt=row.attempt,
            stream=row.stream,
            message=row.message,
            recorded_at=row.recorded_at,
        )
        for row in rows
    ]


def stream_codex_events_ndjson(
    *,
    session,
    settings,
    psycopg_module,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    channel: str | None = None,
    command: str | None = None,
) -> Iterator[str]:  # noqa: ANN001
    query = select(RunLogEvent).order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id)).limit(500)
    if tenant_id:
        query = query.where(RunLogEvent.tenant_id == tenant_id)
    if project_id:
        query = query.where(RunLogEvent.project_id == project_id)
    if run_id:
        query = query.where(RunLogEvent.run_id == run_id)
    if channel:
        query = query.where(RunLogEvent.channel == channel)
    if command:
        query = query.where(RunLogEvent.command == command)

    rows = session.execute(query).scalars().all()
    for row in reversed(rows):
        payload = {
            "event_kind": "codex_log",
            "tenant_id": row.tenant_id,
            "project_id": row.project_id,
            "run_id": row.run_id,
            "issue_key": row.issue_key,
            "agent_id": row.agent_id,
            "invocation_id": row.invocation_id,
            "channel": row.channel,
            "command": row.command,
            "working_dir": row.working_dir,
            "stage": row.stage,
            "attempt": row.attempt,
            "stream": row.stream,
            "message": row.message,
            "recorded_at": row.recorded_at.isoformat(),
        }
        yield json.dumps(payload, separators=(",", ":")) + "\n"

    if psycopg_module is None or not is_postgres_database_url(settings.database_url):
        return

    with psycopg_module.connect(postgres_dsn_from_database_url(settings.database_url), autocommit=True) as conn:
        conn.execute(f'LISTEN "{RUN_EVENT_NOTIFY_CHANNEL}"')
        for notification in conn.notifies():
            try:
                payload = json.loads(str(notification.payload or "{}"))
            except json.JSONDecodeError:
                continue
            if str(payload.get("event_kind") or "") != "codex_log":
                continue
            if tenant_id and str(payload.get("tenant_id") or "") != tenant_id:
                continue
            if project_id and str(payload.get("project_id") or "") != project_id:
                continue
            if run_id and str(payload.get("run_id") or "") != run_id:
                continue
            if channel and str(payload.get("channel") or "") != channel:
                continue
            if command and str(payload.get("command") or "") != command:
                continue
            yield json.dumps(payload, separators=(",", ":")) + "\n"
