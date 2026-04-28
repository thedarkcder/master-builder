from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.product_events import (
    EventCursor,
    ProductEvent,
    list_product_events,
    list_product_events_after_sequence,
)
from orchestrator.core.observability_stream import record_observability_stream_event
from orchestrator.storage.models import RunTokenUsage

EVENT_KIND_RUNTIME_LOG = "runtime_log"
EVENT_KIND_AGENT_LIFECYCLE = "agent_lifecycle"
MAX_LOG_MESSAGE_CHARS = 2000
SUPPORTED_TURN_COMPLETED_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens")
_TOKEN_STAGES = {"pm", "dev", "test", "review", "orchestrated_run"}
_TURN_USAGE_EVENT_TYPES = {
    "turn.completed",
    "turn.complete",
    "turn_completed",
    "response.completed",
    "response.complete",
    "response_completed",
    "assistant.response",
    "assistant_response",
    "item.completed",
}
_EVENT_TYPE_FIELDS = ("type", "event_type", "event")
_TOKEN_VALUE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "total_tokens",
)
_USAGE_CONTAINER_KEYS = ("payload", "data", "metadata", "result")


@dataclass(frozen=True)
class ParsedTurnUsage:
    turn_id: str | None
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    runtime_ms: int | None
    model: str | None
    command: str | None
    artifact_path: str | None
    output_chars: int | None
    truncated: bool | None


@dataclass(frozen=True)
class LoggingPaneEvent:
    tenant_id: str
    project_id: str | None
    workflow_id: str | None
    operation_id: str | None
    attempt_id: str | None
    run_id: str | None
    issue_key: str | None
    agent_id: str
    invocation_id: str
    channel: str
    command: str
    working_dir: str | None
    stage: str
    attempt: int | None
    stream: str
    message: str
    recorded_at: datetime


def _coerce_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        try:
            parsed = int(normalized, 10)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def _event_type(payload: dict[str, object]) -> str:
    for field in _EVENT_TYPE_FIELDS:
        normalized = str(payload.get(field) or "").strip().lower()
        if normalized:
            return normalized
    return ""


def _has_token_fields(payload: dict[str, object]) -> bool:
    return any(value is not None for field in _TOKEN_VALUE_FIELDS if (value := payload.get(field)) is not None)


def _lookup_usage_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    direct_usage = payload.get("usage")
    if isinstance(direct_usage, dict):
        return direct_usage, [payload]

    candidates: list[dict[str, object]] = [payload]
    if _has_token_fields(payload):
        return payload, candidates

    for container_key in _USAGE_CONTAINER_KEYS:
        container = payload.get(container_key)
        if not isinstance(container, dict):
            continue
        candidates.append(container)
        nested_usage = container.get("usage")
        if isinstance(nested_usage, dict):
            return nested_usage, candidates
    return None, candidates


def _first_string_value(candidates: list[dict[str, object]], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        for candidate in candidates:
            value = candidate.get(key)
            if value is None:
                continue
            normalized = str(value).strip()
            if normalized:
                return normalized
    return None


def _first_int_value(candidates: list[dict[str, object]], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        for candidate in candidates:
            value = candidate.get(key)
            coerced = _coerce_non_negative_int(value)
            if coerced is not None:
                return coerced
    return None


def extract_turn_completed_usage(message: str) -> ParsedTurnUsage | None:
    try:
        payload = json.loads(str(message or "").strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    event = _event_type(payload)
    if event not in _TURN_USAGE_EVENT_TYPES:
        return None
    usage_payload, lookup_candidates = _lookup_usage_payload(payload)
    if usage_payload is None or not _has_token_fields(usage_payload):
        return None

    normalized_input = _coerce_non_negative_int(usage_payload.get("input_tokens"))
    normalized_cached = _coerce_non_negative_int(usage_payload.get("cached_input_tokens"))
    normalized_output = _coerce_non_negative_int(usage_payload.get("output_tokens"))
    if normalized_input is None and normalized_output is None and normalized_cached is None:
        normalized_input = _coerce_non_negative_int(usage_payload.get("prompt_tokens"))
        normalized_output = _coerce_non_negative_int(usage_payload.get("completion_tokens"))
        if normalized_cached is None:
            normalized_cached = _coerce_non_negative_int(usage_payload.get("cache_read_tokens"))
    if normalized_input is None and normalized_output is None:
        return None
    if normalized_cached is None:
        normalized_cached = 0

    truncated = payload.get("truncated")
    if not isinstance(truncated, bool):
        for candidate in lookup_candidates:
            candidate_truncated = candidate.get("truncated")
            if isinstance(candidate_truncated, bool):
                truncated = candidate_truncated
                break
        if not isinstance(truncated, bool):
            truncated = None

    return ParsedTurnUsage(
        turn_id=_first_string_value(lookup_candidates, ("turn_id", "turn", "turn_id_str", "invocation_id")),
        input_tokens=normalized_input or 0,
        cached_input_tokens=normalized_cached,
        output_tokens=normalized_output or 0,
        runtime_ms=_first_int_value(lookup_candidates, ("runtime_ms", "duration_ms", "elapsed_ms")),
        model=_first_string_value(lookup_candidates, ("model", "model_name")),
        command=_first_string_value(lookup_candidates, ("command", "command_name")),
        artifact_path=_first_string_value(lookup_candidates, ("artifact_path", "artifact", "artifact_file")),
        output_chars=_first_int_value(lookup_candidates, ("output_chars", "output_length", "output_char_count")),
        truncated=truncated,
    )


def _require_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"Logging pane event requires {field_name}")
    return normalized


def normalize_logging_pane_event(
    *,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    attempt_id: str | None = None,
    run_id: str | None,
    issue_key: str | None,
    agent_id: str,
    invocation_id: str | None,
    channel: str | None,
    command: str | None,
    working_dir: str | None,
    stage: str,
    attempt: int | None,
    stream: str,
    message: str,
    recorded_at: datetime | None = None,
) -> LoggingPaneEvent:
    normalized_operation_id = str(operation_id or "").strip() or None
    normalized_attempt_id = str(attempt_id or "").strip() or None
    if normalized_operation_id is not None and normalized_attempt_id is None:
        raise ValueError("Operation-scoped logging pane events require attempt_id")
    normalized_stream = str(stream or "").strip().lower() or "stdout"
    if normalized_stream not in {"stdout", "stderr", "system"}:
        raise ValueError(f"Unsupported logging pane stream: {normalized_stream}")
    message_text = redact_sensitive_text(str(message or "").strip())[:MAX_LOG_MESSAGE_CHARS]
    if not message_text:
        raise ValueError("Logging pane event requires message")
    return LoggingPaneEvent(
        tenant_id=_require_text(tenant_id, "tenant_id"),
        project_id=str(project_id or "").strip() or None,
        workflow_id=str(workflow_id or "").strip() or None,
        operation_id=normalized_operation_id,
        attempt_id=normalized_attempt_id,
        run_id=str(run_id or "").strip() or None,
        issue_key=str(issue_key or "").strip() or None,
        agent_id=_require_text(agent_id, "agent_id"),
        invocation_id=_require_text(invocation_id, "invocation_id"),
        channel=_require_text(channel, "channel"),
        command=_require_text(command, "command"),
        working_dir=str(working_dir or "").strip() or None,
        stage=_require_text(stage, "stage").lower(),
        attempt=attempt,
        stream=normalized_stream,
        message=message_text,
        recorded_at=recorded_at or datetime.now(timezone.utc),
    )


def _event_payload(event: LoggingPaneEvent) -> dict[str, object]:
    return {
        "agent_id": event.agent_id,
        "invocation_id": event.invocation_id,
        "channel": event.channel,
        "command": event.command,
        "working_dir": event.working_dir,
        "stage": event.stage,
        "attempt": event.attempt,
        "stream": event.stream,
    }


def _materialize_token_usage_from_event(*, session: Session, event: LoggingPaneEvent) -> bool:
    if not event.run_id or event.stage not in _TOKEN_STAGES:
        return False
    parsed = extract_turn_completed_usage(event.message)
    if parsed is None:
        return False
    parsed_cached = parsed.cached_input_tokens
    if parsed.input_tokens > 0:
        parsed_cached = min(parsed.cached_input_tokens, parsed.input_tokens)
    normalized_turn_id = str(parsed.turn_id or "").strip() or None
    exists = session.query(RunTokenUsage.id).filter(
        RunTokenUsage.run_id == event.run_id,
        RunTokenUsage.invocation_id == event.invocation_id,
        RunTokenUsage.turn_id == normalized_turn_id,
        RunTokenUsage.recorded_at == event.recorded_at,
    )
    if exists.first() is not None:
        return False
    session.add(
        RunTokenUsage(
            run_id=event.run_id,
            tenant_id=event.tenant_id,
            project_id=event.project_id,
            attempt=event.attempt,
            stage=event.stage,
            invocation_id=event.invocation_id,
            turn_id=normalized_turn_id,
            recorded_at=event.recorded_at,
            input_tokens=parsed.input_tokens,
            cached_input_tokens=parsed_cached,
            output_tokens=parsed.output_tokens,
            delta_input=None,
            delta_uncached=None,
            delta_output=None,
            runtime_ms=parsed.runtime_ms,
            model=parsed.model,
            command=parsed.command or event.command,
            artifact_path=parsed.artifact_path,
            output_chars=parsed.output_chars,
            truncated=parsed.truncated,
        )
    )
    return True


def materialize_token_usage_from_observability_event(*, session: Session, row: ProductEvent) -> bool:
    payload = dict(row.payload_json or {})
    if row.event_kind != EVENT_KIND_RUNTIME_LOG:
        return False
    event = LoggingPaneEvent(
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        workflow_id=row.workflow_id,
        operation_id=row.operation_id,
        attempt_id=row.attempt_id,
        run_id=row.run_id,
        issue_key=row.issue_key,
        agent_id=_require_text(payload.get("agent_id"), "agent_id"),
        invocation_id=_require_text(payload.get("invocation_id"), "invocation_id"),
        channel=_require_text(payload.get("channel"), "channel"),
        command=_require_text(payload.get("command"), "command"),
        working_dir=str(payload.get("working_dir") or "").strip() or None,
        stage=_require_text(payload.get("stage"), "stage"),
        attempt=payload.get("attempt") if isinstance(payload.get("attempt"), int) else None,
        stream=_require_text(payload.get("stream"), "stream"),
        message=row.message,
        recorded_at=row.recorded_at,
    )
    return _materialize_token_usage_from_event(session=session, event=event)


def emit_logging_pane_event(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    attempt_id: str | None = None,
    run_id: str | None,
    issue_key: str | None,
    agent_id: str,
    invocation_id: str | None,
    channel: str | None,
    command: str | None,
    working_dir: str | None,
    stage: str,
    attempt: int | None,
    stream: str,
    message: str,
    recorded_at: datetime | None = None,
) -> ProductEvent:
    event = normalize_logging_pane_event(
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id=workflow_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        run_id=run_id,
        issue_key=issue_key,
        agent_id=agent_id,
        invocation_id=invocation_id,
        channel=channel,
        command=command,
        working_dir=working_dir,
        stage=stage,
        attempt=attempt,
        stream=stream,
        message=message,
        recorded_at=recorded_at,
    )
    row = record_observability_stream_event(
        session,
        tenant_id=event.tenant_id,
        project_id=event.project_id,
        workflow_id=event.workflow_id,
        run_id=event.run_id,
        operation_id=event.operation_id,
        attempt_id=event.attempt_id,
        issue_key=event.issue_key,
        event_kind=EVENT_KIND_RUNTIME_LOG,
        level="error" if event.stream == "stderr" else "info",
        source_component="logging_pane",
        message=event.message,
        payload=_event_payload(event),
        recorded_at=event.recorded_at,
    )
    if row is None:
        raise RuntimeError("Logging pane event was not persisted")
    _materialize_token_usage_from_event(session=session, event=event)
    return row


def emit_logging_pane_events_batch(*, session: Session, events: Iterable[dict[str, object]]) -> int:
    persisted = 0
    for event in events:
        emit_logging_pane_event(
            session=session,
            tenant_id=str(event.get("tenant_id") or ""),
            project_id=event.get("project_id") if isinstance(event.get("project_id"), str) else None,
            workflow_id=event.get("workflow_id") if isinstance(event.get("workflow_id"), str) else None,
            operation_id=event.get("operation_id") if isinstance(event.get("operation_id"), str) else None,
            attempt_id=event.get("attempt_id") if isinstance(event.get("attempt_id"), str) else None,
            run_id=event.get("run_id") if isinstance(event.get("run_id"), str) else None,
            issue_key=event.get("issue_key") if isinstance(event.get("issue_key"), str) else None,
            agent_id=str(event.get("agent_id") or ""),
            invocation_id=event.get("invocation_id") if isinstance(event.get("invocation_id"), str) else None,
            channel=event.get("channel") if isinstance(event.get("channel"), str) else None,
            command=event.get("command") if isinstance(event.get("command"), str) else None,
            working_dir=event.get("working_dir") if isinstance(event.get("working_dir"), str) else None,
            stage=str(event.get("stage") or ""),
            attempt=event.get("attempt") if isinstance(event.get("attempt"), int) else None,
            stream=str(event.get("stream") or ""),
            message=str(event.get("message") or ""),
            recorded_at=event.get("recorded_at") if isinstance(event.get("recorded_at"), datetime) else None,
        )
        persisted += 1
    return persisted


def emit_agent_lifecycle_log_event(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None,
    run_id: str,
    issue_key: str | None,
    agent_id: str,
    event_type: str,
    recorded_at: datetime,
) -> ProductEvent:
    row = record_observability_stream_event(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id=workflow_id,
        run_id=run_id,
        operation_id=None,
        attempt_id=None,
        issue_key=issue_key,
        event_kind=EVENT_KIND_AGENT_LIFECYCLE,
        level="info",
        source_component="agent_observability",
        message=f"Agent lifecycle event: {event_type}",
        payload={"agent_id": agent_id, "event_type": event_type},
        recorded_at=recorded_at,
    )
    if row is None:
        raise RuntimeError("Agent lifecycle logging pane event was not persisted")
    return row


def logging_pane_event_to_read_schema(row: ProductEvent, schema_cls):  # noqa: ANN001
    payload = dict(row.payload_json or {})
    return schema_cls(
        event_id=str(row.stream_offset),
        event_sequence=row.event_sequence,
        run_id=row.run_id,
        issue_key=row.issue_key,
        project_id=row.project_id,
        agent_id=str(payload.get("agent_id") or "").strip(),
        invocation_id=str(payload.get("invocation_id") or "").strip() or None,
        channel=str(payload.get("channel") or "").strip() or None,
        command=str(payload.get("command") or "").strip() or None,
        working_dir=str(payload.get("working_dir") or "").strip() or None,
        stage=str(payload.get("stage") or "").strip(),
        attempt=payload.get("attempt") if isinstance(payload.get("attempt"), int) else None,
        stream=str(payload.get("stream") or "").strip(),
        message=row.message,
        recorded_at=row.recorded_at,
    )


def list_run_logging_pane_events(
    *,
    session: Session,
    run_id: str,
    schema_cls,  # noqa: ANN001
    limit: int = 200,
    before_recorded_at: datetime | None = None,
    before_event_id: str | None = None,
):
    del session
    rows = list_product_events(
        event_class="execution_log",
        filters={"run_id": str(run_id or "").strip(), "event_kind": EVENT_KIND_RUNTIME_LOG},
        before=EventCursor(
            recorded_at=before_recorded_at,
            event_sequence=_event_sequence_from_id(before_event_id),
        ),
        limit=max(1, min(limit, 1000)),
        newest_first=True,
    )
    return [logging_pane_event_to_read_schema(row, schema_cls) for row in rows]


def list_runtime_logging_pane_events(
    *,
    session: Session,
    schema_cls,  # noqa: ANN001
    tenant_id: str | None = None,
    project_id: str | None = None,
    run_id: str | None = None,
    channel: str | None = None,
    command: str | None = None,
    limit: int = 500,
):
    del session
    filters = {
        "event_kind": EVENT_KIND_RUNTIME_LOG,
        "tenant_id": tenant_id,
        "project_id": project_id,
        "run_id": run_id,
    }
    rows = [
        row
        for row in list_product_events(
            event_class="execution_log",
            filters=filters,
            limit=max(1, min(limit, 2000)),
            newest_first=True,
        )
        if (not channel or str(row.payload_json.get("channel") or "") == channel)
        and (not command or str(row.payload_json.get("command") or "") == command)
    ]
    return [logging_pane_event_to_read_schema(row, schema_cls) for row in rows]


def encode_logging_pane_stream_row(row: ProductEvent) -> str | None:
    payload = dict(row.payload_json or {})
    if row.event_kind == EVENT_KIND_AGENT_LIFECYCLE:
        event_type = str(payload.get("event_type") or "").strip()
        agent_id = str(payload.get("agent_id") or "").strip()
        if not event_type or not row.run_id or not agent_id:
            return None
        return json.dumps(
            {
                "event_type": event_type,
                "run_id": row.run_id,
                "issue_key": row.issue_key,
                "project_id": row.project_id,
                "agent_id": agent_id,
                "recorded_at": row.recorded_at.isoformat(),
            },
            separators=(",", ":"),
        ) + "\n"
    if row.event_kind == EVENT_KIND_RUNTIME_LOG:
        return json.dumps(
            {
                "event_kind": EVENT_KIND_RUNTIME_LOG,
                "event_id": str(row.event_sequence),
                "event_sequence": row.event_sequence,
                "invocation_id": str(payload.get("invocation_id") or "").strip() or None,
                "channel": str(payload.get("channel") or "").strip() or None,
                "command": str(payload.get("command") or "").strip() or None,
                "working_dir": str(payload.get("working_dir") or "").strip() or None,
                "run_id": row.run_id,
                "issue_key": row.issue_key,
                "project_id": row.project_id,
                "agent_id": str(payload.get("agent_id") or "").strip() or None,
                "stage": str(payload.get("stage") or "").strip() or None,
                "attempt": payload.get("attempt") if isinstance(payload.get("attempt"), int) else None,
                "stream": str(payload.get("stream") or "").strip() or None,
                "message": row.message,
                "recorded_at": row.recorded_at.isoformat(),
            },
            separators=(",", ":"),
        ) + "\n"
    return None


def build_run_logging_stream_snapshot_query(
    *,
    session: Session,
    run_id: str,
    initial_event_limit: int,
    initial_log_limit: int,
    ) -> list[ProductEvent]:
    del session
    lifecycle_rows = list_product_events(
        event_class="execution_log",
        filters={"run_id": str(run_id or "").strip(), "event_kind": EVENT_KIND_AGENT_LIFECYCLE},
        limit=max(1, min(initial_event_limit, 500)),
        newest_first=True,
    )
    log_rows = list_product_events(
        event_class="execution_log",
        filters={"run_id": str(run_id or "").strip(), "event_kind": EVENT_KIND_RUNTIME_LOG},
        limit=max(1, min(initial_log_limit, 1000)),
        newest_first=True,
    )
    rows = [*lifecycle_rows, *log_rows]
    return sorted(rows, key=lambda row: int(row.event_sequence or 0))


def build_runtime_logging_stream_snapshot_query(
    *,
    session: Session,
    tenant_id: str | None,
    project_id: str | None,
    run_id: str | None,
    channel: str | None,
    command: str | None,
    limit: int,
) -> list[ProductEvent]:
    del session
    rows = list_product_events(
        event_class="execution_log",
        filters={
            "event_kind": EVENT_KIND_RUNTIME_LOG,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "run_id": run_id,
        },
        limit=max(1, min(limit, 2000)),
        newest_first=False,
    )
    return [
        row
        for row in rows
        if (not channel or str(row.payload_json.get("channel") or "") == channel)
        and (not command or str(row.payload_json.get("command") or "") == command)
    ]


def list_logging_pane_events_after_sequence(
    *,
    filters: dict[str, str | None],
    after_sequence: int,
    limit: int,
) -> list[ProductEvent]:
    return list_product_events_after_sequence(
        event_class="execution_log",
        filters=filters,
        after_sequence=after_sequence,
        limit=limit,
    )


def _event_sequence_from_id(event_id: str | None) -> int | None:
    normalized = str(event_id or "").strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError:
        return None


__all__ = [
    "EVENT_KIND_AGENT_LIFECYCLE",
    "EVENT_KIND_RUNTIME_LOG",
    "ParsedTurnUsage",
    "build_runtime_logging_stream_snapshot_query",
    "build_run_logging_stream_snapshot_query",
    "emit_agent_lifecycle_log_event",
    "emit_logging_pane_event",
    "emit_logging_pane_events_batch",
    "encode_logging_pane_stream_row",
    "extract_turn_completed_usage",
    "list_runtime_logging_pane_events",
    "list_run_logging_pane_events",
    "materialize_token_usage_from_observability_event",
]
