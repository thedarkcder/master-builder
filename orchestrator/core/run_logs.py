from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.log_event_bus import EVENT_KIND_CODEX_LOG, register_stream_offsets
from orchestrator.storage.models import RunLogEvent, RunStreamEvent, RunTokenUsage

MAX_PERSISTED_LOG_EVENTS_PER_RUN = 5000
MAX_LOG_MESSAGE_CHARS = 2000
SUPPORTED_TURN_COMPLETED_KEYS = ("input_tokens", "output_tokens", "cached_input_tokens")
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
_USAGE_FALLBACK_CONTAINER_KEYS = ("payload", "data", "metadata", "result")


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


def _lookup_usage_payload(
    payload: dict[str, object],
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    direct_usage = payload.get("usage")
    if isinstance(direct_usage, dict):
        return direct_usage, [payload]

    candidates: list[dict[str, object]] = [payload]
    if _has_token_fields(payload):
        return payload, candidates

    for container_key in _USAGE_FALLBACK_CONTAINER_KEYS:
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


def _has_token_fields(payload: dict[str, object]) -> bool:
    return any(value is not None for field in _TOKEN_VALUE_FIELDS if (value := payload.get(field)) is not None)


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
    if usage_payload is None:
        return None
    if not _has_token_fields(usage_payload):
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

    turn_id = _first_string_value(
        lookup_candidates,
        ("turn_id", "turn", "turn_id_str", "invocation_id"),
    )
    runtime_ms = _first_int_value(
        lookup_candidates,
        ("runtime_ms", "duration_ms", "elapsed_ms"),
    )
    model = _first_string_value(
        lookup_candidates,
        ("model", "model_name"),
    )
    command = _first_string_value(
        lookup_candidates,
        ("command", "command_name"),
    )
    artifact_path = _first_string_value(
        lookup_candidates,
        ("artifact_path", "artifact", "artifact_file"),
    )
    output_chars = _first_int_value(
        lookup_candidates,
        ("output_chars", "output_length", "output_char_count"),
    )
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
        turn_id=turn_id,
        input_tokens=normalized_input,
        cached_input_tokens=normalized_cached,
        output_tokens=normalized_output,
        runtime_ms=runtime_ms,
        model=model,
        command=command,
        artifact_path=artifact_path,
        output_chars=output_chars,
        truncated=truncated,
    )


def _build_run_token_usage(
    *,
    run_log_row: NormalizedRunLogEvent,
    turn_usage: ParsedTurnUsage,
) -> RunTokenUsage:
    return RunTokenUsage(
        run_id=str(run_log_row.run_id or ""),
        tenant_id=run_log_row.tenant_id,
        project_id=run_log_row.project_id,
        attempt=run_log_row.attempt,
        stage=run_log_row.stage,
        invocation_id=run_log_row.invocation_id,
        turn_id=turn_usage.turn_id,
        recorded_at=run_log_row.recorded_at,
        input_tokens=turn_usage.input_tokens,
        cached_input_tokens=turn_usage.cached_input_tokens,
        output_tokens=turn_usage.output_tokens,
        delta_input=None,
        delta_uncached=None,
        delta_output=None,
        runtime_ms=turn_usage.runtime_ms,
        model=turn_usage.model,
        command=turn_usage.command or run_log_row.command,
        artifact_path=turn_usage.artifact_path,
        output_chars=turn_usage.output_chars,
        truncated=turn_usage.truncated,
    )


def materialize_token_usage_from_log_message(
    *,
    session,
    run_log_row: NormalizedRunLogEvent,
) -> bool:
    if not run_log_row.run_id:
        return False
    if str(run_log_row.stage or "").strip().lower() not in {
        "pm",
        "dev",
        "test",
        "review",
        "orchestrated_run",
    }:
        return False
    parsed = extract_turn_completed_usage(run_log_row.message)
    if parsed is None:
        return False
    if parsed.input_tokens < 0 or parsed.output_tokens < 0 or parsed.cached_input_tokens < 0:
        return False
    parsed_cached = parsed.cached_input_tokens
    if parsed.input_tokens > 0:
        parsed_cached = min(parsed.cached_input_tokens, parsed.input_tokens)
    # Persist best-effort to avoid pipeline stalls when token extraction is malformed.
    try:
        # Unique by run_id/invocation_id/turn_id/recorded_at to keep ingestion idempotent.
        normalized_turn_id = str(parsed.turn_id or "").strip() or None
        normalized_invocation = str(run_log_row.invocation_id or "").strip() or None
        exists_query = session.query(RunTokenUsage.id).filter(
            RunTokenUsage.run_id == str(run_log_row.run_id),
            RunTokenUsage.invocation_id == normalized_invocation,
            RunTokenUsage.turn_id == normalized_turn_id,
            RunTokenUsage.recorded_at == run_log_row.recorded_at,
        )
        if exists_query.first() is not None:
            return False
        token_record = _build_run_token_usage(
            run_log_row=run_log_row,
            turn_usage=ParsedTurnUsage(
                turn_id=parsed.turn_id,
                input_tokens=parsed.input_tokens,
                cached_input_tokens=parsed_cached,
                output_tokens=parsed.output_tokens,
                runtime_ms=parsed.runtime_ms,
                model=parsed.model,
                command=parsed.command,
                artifact_path=parsed.artifact_path,
                output_chars=parsed.output_chars,
                truncated=parsed.truncated,
            ),
        )
        session.add(token_record)
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class NormalizedRunLogEvent:
    tenant_id: str
    project_id: str | None
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


def _normalize_run_log_event(
    *,
    tenant_id: str,
    project_id: str | None,
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
) -> NormalizedRunLogEvent | None:
    normalized_tenant = str(tenant_id or "").strip()
    if not normalized_tenant:
        raise ValueError("Codex log event requires tenant_id")
    normalized_message = str(message or "").strip()
    if not normalized_message:
        return None
    normalized_message = redact_sensitive_text(normalized_message)
    normalized_message = normalized_message[:MAX_LOG_MESSAGE_CHARS]
    timestamp = recorded_at or datetime.now(timezone.utc)
    normalized_stream = str(stream or "").strip().lower() or "stdout"
    if normalized_stream not in {"stdout", "stderr", "system"}:
        normalized_stream = "stdout"
    normalized_stage = str(stage or "").strip().lower()
    normalized_agent = str(agent_id or "").strip()
    normalized_run_id = str(run_id or "").strip() or None
    normalized_invocation_id = str(invocation_id or "").strip() or None
    normalized_channel = str(channel or "").strip() or None
    normalized_command = str(command or "").strip() or None
    normalized_working_dir = str(working_dir or "").strip() or None
    if not normalized_stage:
        raise ValueError("Codex log event requires stage")
    if not normalized_agent:
        raise ValueError("Codex log event requires agent_id")
    if normalized_invocation_id is None:
        raise ValueError("Codex log event requires invocation_id")
    if normalized_channel is None:
        raise ValueError("Codex log event requires channel")
    if normalized_command is None:
        raise ValueError("Codex log event requires command")
    return NormalizedRunLogEvent(
        tenant_id=normalized_tenant,
        project_id=str(project_id or "").strip() or None,
        run_id=normalized_run_id,
        issue_key=str(issue_key or "").strip() or None,
        agent_id=normalized_agent,
        invocation_id=normalized_invocation_id,
        channel=normalized_channel,
        command=normalized_command,
        working_dir=normalized_working_dir,
        stage=normalized_stage,
        attempt=attempt,
        stream=normalized_stream,
        message=normalized_message,
        recorded_at=timestamp,
    )


def _build_run_log_model(event: NormalizedRunLogEvent) -> RunLogEvent:
    return RunLogEvent(
        event_id=uuid4().hex,
        tenant_id=event.tenant_id,
        project_id=event.project_id,
        run_id=event.run_id,
        issue_key=event.issue_key,
        agent_id=event.agent_id,
        invocation_id=event.invocation_id,
        channel=event.channel,
        command=event.command,
        working_dir=event.working_dir,
        stage=event.stage,
        attempt=event.attempt,
        stream=event.stream,
        message=event.message,
        recorded_at=event.recorded_at,
    )


def _build_run_stream_model(event: NormalizedRunLogEvent) -> RunStreamEvent:
    return RunStreamEvent(
        event_kind=EVENT_KIND_CODEX_LOG,
        tenant_id=event.tenant_id,
        run_id=event.run_id,
        issue_key=event.issue_key,
        project_id=event.project_id,
        agent_id=event.agent_id,
        event_type=None,
        invocation_id=event.invocation_id,
        channel=event.channel,
        command=event.command,
        working_dir=event.working_dir,
        stage=event.stage,
        attempt=event.attempt,
        stream=event.stream,
        message=event.message,
        recorded_at=event.recorded_at,
    )


def record_run_log_event(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
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
    max_events_per_run: int = MAX_PERSISTED_LOG_EVENTS_PER_RUN,
) -> None:
    normalized_event = _normalize_run_log_event(
        tenant_id=tenant_id,
        project_id=project_id,
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
    if normalized_event is None:
        return
    log_row = _build_run_log_model(normalized_event)
    stream_row = _build_run_stream_model(normalized_event)
    session.add(log_row)
    session.add(stream_row)
    materialize_token_usage_from_log_message(session=session, run_log_row=normalized_event)
    session.flush()
    register_stream_offsets(session=session, rows=[stream_row])


def record_run_log_events_batch(
    *,
    session: Session,
    events: Iterable[dict[str, object]],
    max_events_per_run: int = MAX_PERSISTED_LOG_EVENTS_PER_RUN,
) -> int:
    normalized_events: list[NormalizedRunLogEvent] = []
    for event in events:
        normalized = _normalize_run_log_event(
            tenant_id=str(event.get("tenant_id") or ""),
            project_id=event.get("project_id") if isinstance(event.get("project_id"), str) else None,
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
        if normalized is not None:
            normalized_events.append(normalized)
    if not normalized_events:
        return 0

    stream_rows: list[RunStreamEvent] = []
    for normalized_event in normalized_events:
        session.add(_build_run_log_model(normalized_event))
        stream_row = _build_run_stream_model(normalized_event)
        stream_rows.append(stream_row)
        session.add(stream_row)
        materialize_token_usage_from_log_message(session=session, run_log_row=normalized_event)
    session.flush()
    register_stream_offsets(session=session, rows=stream_rows)
    return len(normalized_events)


def prune_run_log_events(
    *,
    session: Session,
    max_events_per_run: int = MAX_PERSISTED_LOG_EVENTS_PER_RUN,
) -> int:
    ranked = (
        select(
            RunLogEvent.event_id,
            func.row_number()
            .over(
                partition_by=RunLogEvent.invocation_id,
                order_by=(RunLogEvent.recorded_at.desc(), RunLogEvent.event_id.desc()),
            )
            .label("row_number"),
        )
        .where(RunLogEvent.invocation_id.is_not(None))
        .subquery()
    )
    cutoff_ids = session.execute(
        select(ranked.c.event_id).where(ranked.c.row_number > max(0, int(max_events_per_run)))
    ).scalars().all()
    if not cutoff_ids:
        return 0
    result = session.execute(delete(RunLogEvent).where(RunLogEvent.event_id.in_(cutoff_ids)))
    return int(result.rowcount or 0)


def prune_run_stream_events(
    *,
    session: Session,
    max_events_per_run: int = MAX_PERSISTED_LOG_EVENTS_PER_RUN,
) -> int:
    ranked_logs = (
        select(
            RunStreamEvent.stream_offset,
            func.row_number()
            .over(
                partition_by=RunStreamEvent.invocation_id,
                order_by=RunStreamEvent.stream_offset.desc(),
            )
            .label("row_number"),
        )
        .where(
            RunStreamEvent.event_kind == EVENT_KIND_CODEX_LOG,
            RunStreamEvent.invocation_id.is_not(None),
        )
        .subquery()
    )
    cutoff_ids = session.execute(
        select(ranked_logs.c.stream_offset).where(ranked_logs.c.row_number > max(0, int(max_events_per_run)))
    ).scalars().all()
    if not cutoff_ids:
        return 0
    result = session.execute(delete(RunStreamEvent).where(RunStreamEvent.stream_offset.in_(cutoff_ids)))
    return int(result.rowcount or 0)
