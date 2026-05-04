from __future__ import annotations

from collections.abc import Iterator

from orchestrator.core.observability.stream import stream_product_event_rows
from orchestrator.core.observability.logging_pane import (
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
    filters = {
        "event_kind": "runtime_log",
        "tenant_id": tenant_id,
        "project_id": project_id,
        "run_id": run_id,
    }

    def _encode_runtime_row(row):
        if channel and str(row.payload_json.get("channel") or "") != channel:
            return None
        if command and str(row.payload_json.get("command") or "") != command:
            return None
        return encode_logging_pane_stream_row(row)

    yield from stream_product_event_rows(
        snapshot_loader=lambda: build_runtime_logging_stream_snapshot_query(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            channel=channel,
            command=command,
            limit=500,
        ),
        after_cursor_loader=lambda cursor: list_logging_pane_events_after_sequence(
            filters=filters,
            after_sequence=cursor,
            limit=500,
        ),
        encoder=_encode_runtime_row,
        batch_limit=500,
    )
