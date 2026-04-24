from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select

from orchestrator.storage.models import (
    AuditEvent,
    DecisionEvent,
    FollowupContext,
    Run,
    RunHumanInputRequest,
    WorkflowCheckpoint,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)


def latest_checkpoint_for_kind(*, session, workflow_id: str, checkpoint_kind: str) -> WorkflowCheckpoint | None:  # noqa: ANN001
    normalized_kind = str(checkpoint_kind or "").strip().lower()
    accepted_kinds = (
        ("pm",)
        if normalized_kind == "pm"
        else ("execution", "orchestrated")
        if normalized_kind == "execution"
        else (normalized_kind,)
    )
    return session.execute(
        select(WorkflowCheckpoint)
        .where(
            WorkflowCheckpoint.workflow_id == workflow_id,
            WorkflowCheckpoint.checkpoint_kind.in_(accepted_kinds),
        )
        .order_by(desc(WorkflowCheckpoint.created_at))
        .limit(1)
    ).scalar_one_or_none()


def pending_input_request(*, session, workflow_id: str) -> RunHumanInputRequest | None:  # noqa: ANN001
    return session.execute(
        select(RunHumanInputRequest)
        .where(
            RunHumanInputRequest.workflow_id == workflow_id,
            RunHumanInputRequest.status == "pending",
        )
        .order_by(desc(RunHumanInputRequest.created_at))
        .limit(1)
    ).scalar_one_or_none()


def workflow_checkpoint_kinds(*, session, workflow_id: str) -> list[str]:  # noqa: ANN001
    checkpoint_kinds = session.execute(
        select(WorkflowCheckpoint.checkpoint_kind)
        .where(WorkflowCheckpoint.workflow_id == workflow_id)
        .order_by(desc(WorkflowCheckpoint.created_at))
    ).scalars().all()
    seen: set[str] = set()
    ordered: list[str] = []
    for kind in checkpoint_kinds:
        normalized = str(kind or "").strip().lower()
        if normalized and normalized not in seen:
            ordered.append(normalized)
            seen.add(normalized)
    return ordered


def active_followup_contexts(*, session, tenant_id: str, issue_key: str) -> list[FollowupContext]:  # noqa: ANN001
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_issue_key = str(issue_key or "").strip().upper()
    if not normalized_tenant_id or not normalized_issue_key:
        return []
    return session.execute(
        select(FollowupContext)
        .where(
            FollowupContext.tenant_id == normalized_tenant_id,
            FollowupContext.issue_key == normalized_issue_key,
            FollowupContext.status == "active",
        )
        .order_by(desc(FollowupContext.updated_at))
    ).scalars().all()


def workflow_runs(*, session, workflow_id: str) -> list[Run]:  # noqa: ANN001
    return session.execute(
        select(Run)
        .where(Run.workflow_id == workflow_id)
        .order_by(Run.attempt_number.asc())
    ).scalars().all()


def workflow_operations(*, session, workflow_id: str) -> list[WorkflowOperation]:  # noqa: ANN001
    return session.execute(
        select(WorkflowOperation)
        .where(WorkflowOperation.workflow_id == workflow_id)
        .order_by(desc(WorkflowOperation.created_at))
    ).scalars().all()


def workflow_operation_attempts_by_operation(*, session, workflow_id: str) -> dict[str, list[WorkflowOperationAttempt]]:  # noqa: ANN001
    rows = session.execute(
        select(WorkflowOperationAttempt, WorkflowOperation)
        .join(WorkflowOperation, WorkflowOperation.operation_id == WorkflowOperationAttempt.operation_id)
        .where(WorkflowOperation.workflow_id == workflow_id)
        .order_by(WorkflowOperationAttempt.attempt_number.asc())
    ).all()
    grouped: dict[str, list[WorkflowOperationAttempt]] = {}
    for attempt, operation in rows:
        grouped.setdefault(operation.operation_id, []).append(attempt)
    return grouped


def workflow_operation_attempts(*, session, operation_id: str) -> list[WorkflowOperationAttempt]:  # noqa: ANN001
    return session.execute(
        select(WorkflowOperationAttempt)
        .where(WorkflowOperationAttempt.operation_id == operation_id)
        .order_by(desc(WorkflowOperationAttempt.attempt_number))
    ).scalars().all()


def audit_events_by_operation(*, session, workflow_id: str) -> dict[str, list[AuditEvent]]:  # noqa: ANN001
    rows = session.execute(
        select(AuditEvent)
        .where(AuditEvent.workflow_id == workflow_id, AuditEvent.operation_id.is_not(None))
        .order_by(AuditEvent.recorded_at.asc(), AuditEvent.event_id.asc())
    ).scalars().all()
    grouped: dict[str, list[AuditEvent]] = {}
    for row in rows:
        operation_id = str(row.operation_id or "").strip()
        if not operation_id:
            continue
        grouped.setdefault(operation_id, []).append(row)
    return grouped


def latest_run_for_workflow(*, session, workflow_id: str) -> Run | None:  # noqa: ANN001
    return session.execute(
        select(Run)
        .where(Run.workflow_id == workflow_id)
        .order_by(desc(Run.attempt_number))
        .limit(1)
    ).scalar_one_or_none()


def latest_resumable_checkpoint(*, session, workflow_id: str) -> WorkflowCheckpoint | None:  # noqa: ANN001
    return session.execute(
        select(WorkflowCheckpoint)
        .where(
            WorkflowCheckpoint.workflow_id == workflow_id,
            WorkflowCheckpoint.checkpoint_kind.in_(("pm", "execution", "orchestrated")),
        )
        .order_by(desc(WorkflowCheckpoint.created_at))
        .limit(1)
    ).scalar_one_or_none()


def workflow_by_execution_id(*, session, execution_id: str) -> WorkflowExecution | None:  # noqa: ANN001
    normalized_execution_id = str(execution_id or "").strip()
    if not normalized_execution_id:
        return None
    return session.execute(
        select(WorkflowExecution)
        .where(WorkflowExecution.execution_id == normalized_execution_id)
        .limit(1)
    ).scalar_one_or_none()


def latest_decision_issue_labels_for_workflow(*, session, workflow) -> list[str]:  # noqa: ANN001
    event = session.execute(
        select(DecisionEvent)
        .where(
            DecisionEvent.tenant_id == workflow.tenant_id,
            DecisionEvent.issue_key == workflow.source_ref,
        )
        .order_by(desc(DecisionEvent.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if event is None or not isinstance(event.payload_json, dict):
        return []
    labels = event.payload_json.get("issue_labels", [])
    if not isinstance(labels, list):
        return []
    return [str(label).strip() for label in labels if str(label).strip()]


def cursor_filtered_events(
    *,
    events: list,
    before_recorded_at: datetime | None,
    before_event_id: str | None,
    limit: int,
) -> list:
    normalized_before_event_id = str(before_event_id or "").strip()
    filtered: list = []
    for event in events:
        if before_recorded_at is not None:
            if event.recorded_at > before_recorded_at:
                continue
            if (
                event.recorded_at == before_recorded_at
                and normalized_before_event_id
                and event.event_id >= normalized_before_event_id
            ):
                continue
        filtered.append(event)
    filtered.sort(key=lambda item: (item.recorded_at, item.event_id), reverse=True)
    return filtered[: max(1, min(limit, 500))]
