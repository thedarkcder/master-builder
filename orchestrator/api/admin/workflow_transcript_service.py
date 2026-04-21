from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Iterable, Literal

from orchestrator.api.schemas import (
    WorkflowObservabilityEventRead,
    WorkflowOperationAttemptRead,
    WorkflowStepAttemptTranscriptRead,
    WorkflowStepTranscriptRead,
    WorkflowTranscriptEntryRead,
    WorkflowTranscriptSectionRead,
)
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt

_SECTION_LABELS: dict[str, str] = {
    "summary": "Summary",
    "runtime": "Runtime",
    "prompts": "Prompts",
    "tool_calls": "Tool calls",
    "external_requests": "External requests",
    "external_responses": "External responses",
    "outcome": "Outcome",
}

_SECTION_ORDER = [
    "summary",
    "runtime",
    "prompts",
    "tool_calls",
    "external_requests",
    "external_responses",
    "outcome",
]


def build_workflow_step_transcript(
    *,
    session,  # noqa: ANN001
    workflow: WorkflowExecution,
    operation: WorkflowOperation,
    attempts: list[WorkflowOperationAttempt] | list[WorkflowOperationAttemptRead],
    telemetry_events: list[WorkflowObservabilityEventRead],
    audit_events: list[WorkflowObservabilityEventRead],
    source: Literal["audit", "telemetry"],
) -> WorkflowStepTranscriptRead:
    source_events = audit_events if source == "audit" else telemetry_events
    attempt_reads = [_attempt_to_schema(attempt) for attempt in attempts]
    events_by_attempt = _events_by_attempt(events=source_events, attempts=attempt_reads)
    attempt_transcripts = [
        _attempt_transcript(
            workflow=workflow,
            operation=operation,
            attempt=attempt,
            events=events_by_attempt.get(attempt.attempt_id, []),
        )
        for attempt in sorted(attempt_reads, key=lambda item: item.attempt_number, reverse=True)
    ]
    return WorkflowStepTranscriptRead(
        execution_id=workflow.execution_id,
        operation_id=operation.operation_id,
        operation_label=(str(operation.summary or "").strip() and str(operation.summary or "").strip())
        or str(getattr(operation, "label", "") or "").strip()
        or str(operation.operation_type or "").strip(),
        current_status=operation.status,
        source=source,
        attempts=attempt_transcripts,
    )


def _attempt_to_schema(attempt: WorkflowOperationAttempt | WorkflowOperationAttemptRead) -> WorkflowOperationAttemptRead:
    if isinstance(attempt, WorkflowOperationAttemptRead):
        return attempt
    return WorkflowOperationAttemptRead(
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        status=attempt.status,
        error_category=attempt.error_category,
        error_message=attempt.error_message,
        status_detail=attempt.status_detail,
        retryable=attempt.retryable,
        next_retry_at=attempt.next_retry_at,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


def _events_by_attempt(
    *,
    events: Iterable[WorkflowObservabilityEventRead],
    attempts: list[WorkflowOperationAttemptRead],
) -> dict[str, list[WorkflowObservabilityEventRead]]:
    attempt_ids = {attempt.attempt_id for attempt in attempts}
    attempt_numbers = {attempt.attempt_number: attempt.attempt_id for attempt in attempts}
    grouped: dict[str, list[WorkflowObservabilityEventRead]] = defaultdict(list)
    for event in events:
        attempt_id = str(event.attempt_id or "").strip() or None
        if attempt_id is None and isinstance(event.attempt, int):
            attempt_id = attempt_numbers.get(event.attempt)
        if attempt_id is None or attempt_id not in attempt_ids:
            continue
        grouped[attempt_id].append(event)
    for event_list in grouped.values():
        event_list.sort(key=lambda item: (item.recorded_at, item.event_id))
    return grouped


def _attempt_transcript(
    *,
    workflow: WorkflowExecution,
    operation: WorkflowOperation,
    attempt: WorkflowOperationAttemptRead,
    events: list[WorkflowObservabilityEventRead],
) -> WorkflowStepAttemptTranscriptRead:
    section_entries: dict[str, list[WorkflowTranscriptEntryRead]] = defaultdict(list)
    for event in events:
        section_kind = _section_for_event(event)
        if section_kind is None:
            continue
        section_entries[section_kind].append(
            WorkflowTranscriptEntryRead(
                entry_id=event.event_id,
                recorded_at=event.recorded_at,
                level=event.level,
                title=_title_for_event(event=event, section_kind=section_kind),
                message=event.message,
                source_component=event.source_component,
                payload=dict(event.payload or {}),
            )
        )

    outcome_entries = section_entries["outcome"]
    if attempt.error_message or attempt.status_detail or attempt.status:
        outcome_entries.append(
            WorkflowTranscriptEntryRead(
                entry_id=f"attempt:{attempt.attempt_id}:outcome",
                recorded_at=attempt.finished_at or attempt.started_at or workflow.updated_at,
                level="error" if str(attempt.status).strip().lower() == "failed" else "info",
                title="Attempt outcome",
                message=attempt.error_message or attempt.status_detail or str(attempt.status or "").replace("_", " ").strip().title(),
                source_component="workflow_operation_attempt",
                payload={
                    "status": attempt.status,
                    "attempt_number": attempt.attempt_number,
                    "error_category": attempt.error_category,
                    "retryable": attempt.retryable,
                    "next_retry_at": attempt.next_retry_at.isoformat() if attempt.next_retry_at is not None else None,
                },
            )
        )

    sections = [
        WorkflowTranscriptSectionRead(
            kind=section_kind, label=_SECTION_LABELS[section_kind], entries=section_entries[section_kind]
        )
        for section_kind in _SECTION_ORDER
        if section_entries.get(section_kind)
    ]

    return WorkflowStepAttemptTranscriptRead(
        attempt_id=attempt.attempt_id,
        attempt_number=attempt.attempt_number,
        status=attempt.status,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        duration_ms=_duration_ms(started_at=attempt.started_at, finished_at=attempt.finished_at),
        error_category=attempt.error_category,
        failure_message=attempt.error_message,
        status_detail=attempt.status_detail,
        recommended_next_action=_recommended_next_action(operation=operation, attempt=attempt),
        sections=sections,
    )


def _section_for_event(event: WorkflowObservabilityEventRead) -> str | None:
    kind = str(event.event_kind or "").strip().lower()
    if kind in {"stage_request", "stage_response"}:
        return "prompts"
    if kind in {"tool_request", "tool_result"}:
        return "tool_calls"
    if kind.endswith("_request"):
        return "external_requests"
    if kind.endswith("_response"):
        return "external_responses"
    if kind in {
        "workflow_operation_attempt_started",
        "workflow_operation_attempt_retried",
        "workflow_operation_attempt_completed",
    }:
        return "summary"
    if kind in {"workflow_operation_attempt_failed", "attempt_failed"}:
        return "outcome"
    if kind in {
        "stage_invocation_started",
        "stage_invocation_finished",
        "runtime_log",
        "no_assistant_output_event",
        "thread.started",
        "turn.started",
        "turn.completed",
        "thread.completed",
    }:
        return "runtime"
    return None


def _title_for_event(*, event: WorkflowObservabilityEventRead, section_kind: str) -> str:
    if section_kind == "prompts":
        if event.event_kind == "stage_request":
            return "Runtime request"
        if event.event_kind == "stage_response":
            return "Runtime response"
    if section_kind == "tool_calls":
        tool_name = str((event.payload or {}).get("tool_name") or "").strip()
        if tool_name:
            return tool_name
        return event.event_kind.replace("_", " ")
    if section_kind in {"external_requests", "external_responses"}:
        return event.event_kind.replace("_", " ")
    if section_kind == "runtime":
        return event.event_kind.replace("_", " ")
    if section_kind == "summary":
        return "Attempt lifecycle"
    if section_kind == "outcome":
        return "Attempt failure" if str(event.level or "").lower() == "error" else "Attempt outcome"
    return event.event_kind.replace("_", " ")


def _duration_ms(*, started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if started_at is None or finished_at is None:
        return None
    return max(0, int((finished_at - started_at).total_seconds() * 1000))


def _recommended_next_action(*, operation: WorkflowOperation, attempt: WorkflowOperationAttemptRead) -> str | None:
    if attempt.error_message:
        first_line = str(attempt.error_message).splitlines()[0].strip()
        return first_line or None
    if attempt.status_detail:
        first_line = str(attempt.status_detail).splitlines()[0].strip()
        return first_line or None
    if str(attempt.status or "").strip().lower() == "completed":
        return f"{str(operation.operation_type or '').replace('_', ' ').strip().title()} completed."
    return None
