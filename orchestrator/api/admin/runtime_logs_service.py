from __future__ import annotations

from collections.abc import Iterator
from threading import Event

from orchestrator.core.logging_pane_events import (
    build_runtime_logging_stream_snapshot_query,
    encode_logging_pane_stream_row,
    list_logging_pane_events_after_sequence,
    list_runtime_logging_pane_events,
)


def list_runtime_log_events(
    *,
    session,
    logging_pane_schema_cls,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    channel: str | None = None,
    command: str | None = None,
    limit: int = 500,
):  # noqa: ANN001
    return list_runtime_logging_pane_events(
        session=session,
        schema_cls=logging_pane_schema_cls,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
        limit=limit,
    )


def stream_runtime_events_ndjson(
    *,
    session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    channel: str | None = None,
    command: str | None = None,
) -> Iterator[str]:  # noqa: ANN001
    rows = build_runtime_logging_stream_snapshot_query(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
        limit=500,
    )
    cursor = max((int(row.event_sequence) for row in rows), default=0)
    for row in rows:
        payload = encode_logging_pane_stream_row(row)
        if payload is not None:
            yield payload

    filters = {
        "event_kind": "runtime_log",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "run_id": run_id,
    }
    poll_seconds = max(0.1, int(getattr(settings, "event_stream_poll_ms", 500)) / 1000.0)
    stop = Event()
    while not stop.wait(timeout=poll_seconds):
        streamed_rows = list_logging_pane_events_after_sequence(
            filters=filters,
            after_sequence=cursor,
            limit=500,
        )
        for row in streamed_rows:
            if channel and str(row.payload_json.get("channel") or "") != channel:
                continue
            if command and str(row.payload_json.get("command") or "") != command:
                continue
            payload = encode_logging_pane_stream_row(row)
            if payload is not None:
                yield payload
        if streamed_rows:
            cursor = max(int(row.event_sequence) for row in streamed_rows)
