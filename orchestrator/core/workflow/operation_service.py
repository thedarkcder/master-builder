from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from uuid import uuid4

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.observability.audit import record_workflow_operation_audit_event
from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef
from orchestrator.core.workflow.operation_logging import emit_workflow_operation_log
from orchestrator.storage.models import WorkflowOperation, WorkflowOperationAttempt

OPERATION_STATUS_PENDING = "pending"
OPERATION_STATUS_RUNNING = "running"
OPERATION_STATUS_WAITING_FOR_INPUT = "waiting_for_input"
OPERATION_STATUS_RETRYING = "retrying"
OPERATION_STATUS_FAILED = "failed"
OPERATION_STATUS_COMPLETED = "completed"
ACTIVE_OPERATION_ATTEMPT_STATUSES = frozenset({OPERATION_STATUS_RUNNING, OPERATION_STATUS_WAITING_FOR_INPUT})


class WorkflowOperationAttemptAlreadyRunningError(RuntimeError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease_expires_at(anchor: datetime) -> datetime:
    timeout_seconds = max(60, int(getattr(get_settings(), "workflow_operation_attempt_stale_timeout_seconds", 300)))
    return anchor + timedelta(seconds=timeout_seconds)


def _attempt_ref(*, operation: WorkflowOperation, attempt: WorkflowOperationAttempt) -> WorkflowAttemptRef:
    return WorkflowAttemptRef(
        workflow_id=operation.workflow_id,
        operation_id=operation.operation_id,
        attempt_id=attempt.attempt_id,
        number=attempt.attempt_number,
    )


@dataclass(frozen=True)
class WorkflowOperationHandle:
    operation_id: str
    workflow_id: str
    operation_type: str
    status: str


def upsert_workflow_operation(
    session: Session,
    *,
    workflow_id: str,
    operation_type: str,
    idempotency_key: str,
    run_id: str | None = None,
    target_system: str | None = None,
    target_ref: str | None = None,
    summary: str | None = None,
) -> WorkflowOperation:
    existing = session.execute(
        select(WorkflowOperation).where(
            WorkflowOperation.workflow_id == workflow_id,
            WorkflowOperation.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.run_id = run_id or existing.run_id
        existing.target_system = target_system or existing.target_system
        existing.target_ref = target_ref or existing.target_ref
        existing.summary = summary or existing.summary
        existing.updated_at = _now()
        return existing

    now = _now()
    operation = WorkflowOperation(
        operation_id=uuid4().hex,
        workflow_id=workflow_id,
        run_id=run_id,
        operation_type=operation_type,
        idempotency_key=idempotency_key,
        status=OPERATION_STATUS_PENDING,
        target_system=target_system,
        target_ref=target_ref,
        summary=summary,
        created_at=now,
        started_at=None,
        finished_at=None,
        updated_at=now,
    )
    session.add(operation)
    session.flush()
    return operation


def start_workflow_operation_attempt(
    session: Session,
    *,
    operation: WorkflowOperation,
) -> WorkflowOperationAttempt:
    running_attempt = session.execute(
        select(WorkflowOperationAttempt)
        .where(
            WorkflowOperationAttempt.operation_id == operation.operation_id,
            WorkflowOperationAttempt.status.in_(ACTIVE_OPERATION_ATTEMPT_STATUSES),
        )
        .order_by(desc(WorkflowOperationAttempt.attempt_number))
        .limit(1)
    ).scalar_one_or_none()
    if running_attempt is not None:
        raise WorkflowOperationAttemptAlreadyRunningError(
            "Workflow operation "
            f"{operation.operation_type} already has active attempt {running_attempt.attempt_number} "
            f"({running_attempt.attempt_id})."
        )
    next_attempt_number = int(
        session.execute(
            select(func.max(WorkflowOperationAttempt.attempt_number)).where(
                WorkflowOperationAttempt.operation_id == operation.operation_id
            )
        ).scalar_one()
        or 0
    ) + 1
    now = _now()
    operation.status = OPERATION_STATUS_RUNNING
    operation.started_at = operation.started_at or now
    operation.finished_at = None
    operation.updated_at = now
    attempt = WorkflowOperationAttempt(
        attempt_id=uuid4().hex,
        operation_id=operation.operation_id,
        attempt_number=next_attempt_number,
        status=OPERATION_STATUS_RUNNING,
        error_category=None,
        error_message=None,
        status_detail=None,
        retryable=False,
        next_retry_at=None,
        last_heartbeat_at=now,
        lease_expires_at=_lease_expires_at(now),
        lease_owner=str(getattr(get_settings(), "agent_id", "") or "workflow_operation_service"),
        created_at=now,
        started_at=now,
        finished_at=None,
    )
    session.add(attempt)
    session.flush()
    record_workflow_operation_audit_event(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        source_component="workflow_operation_service",
        event_kind="attempt_started",
        level="info",
        message=f"Started {operation.operation_type} attempt {next_attempt_number}.",
        payload={"status": attempt.status, "attempt_number": next_attempt_number},
    )
    emit_workflow_operation_log(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        event_type="workflow_operation_attempt_started",
        message=f"Started {operation.operation_type} attempt {next_attempt_number}.",
        metadata={"status": attempt.status},
    )
    return attempt


def fail_active_workflow_operation_attempts_for_run(
    session: Session,
    *,
    run_id: str,
    error_message: str,
    now: datetime | None = None,
) -> int:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return 0
    timestamp = now or _now()
    rows = session.execute(
        select(WorkflowOperation, WorkflowOperationAttempt)
        .join(WorkflowOperationAttempt, WorkflowOperationAttempt.operation_id == WorkflowOperation.operation_id)
        .where(
            WorkflowOperation.run_id == normalized_run_id,
            WorkflowOperationAttempt.status.in_(ACTIVE_OPERATION_ATTEMPT_STATUSES),
        )
    ).all()
    for operation, attempt in rows:
        operation.status = OPERATION_STATUS_FAILED
        operation.finished_at = timestamp
        operation.updated_at = timestamp
        attempt.status = OPERATION_STATUS_FAILED
        attempt.error_category = "run_attempt_reset"
        attempt.error_message = error_message
        attempt.retryable = True
        attempt.next_retry_at = None
        attempt.last_heartbeat_at = timestamp
        attempt.lease_expires_at = timestamp
        attempt.finished_at = timestamp
        record_workflow_operation_audit_event(
            session,
            operation=operation,
            attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
            source_component="workflow_operation_service",
            event_kind="attempt_failed",
            level="warning",
            message=error_message,
            payload={"status": attempt.status, "error_category": attempt.error_category},
        )
        emit_workflow_operation_log(
            session,
            operation=operation,
            attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
            event_type="workflow_operation_attempt_failed",
            message=error_message,
            metadata={"status": attempt.status, "error_category": attempt.error_category},
        )
    return len(rows)


def touch_workflow_operation_attempt_heartbeat(
    session: Session,
    *,
    attempt: WorkflowOperationAttempt,
    lease_owner: str,
    heartbeat_at: datetime | None = None,
) -> None:
    timestamp = heartbeat_at or _now()
    attempt.last_heartbeat_at = timestamp
    attempt.lease_expires_at = _lease_expires_at(timestamp)
    attempt.lease_owner = lease_owner
    session.flush()


def complete_workflow_operation(
    session: Session,
    *,
    operation: WorkflowOperation,
    attempt: WorkflowOperationAttempt,
    summary: str | None = None,
) -> None:
    now = _now()
    operation.status = OPERATION_STATUS_COMPLETED
    operation.summary = summary or operation.summary
    operation.finished_at = now
    operation.updated_at = now
    attempt.status = OPERATION_STATUS_COMPLETED
    attempt.status_detail = None
    attempt.lease_expires_at = None
    attempt.finished_at = now
    record_workflow_operation_audit_event(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        source_component="workflow_operation_service",
        event_kind="attempt_completed",
        level="info",
        message=summary or f"Completed {operation.operation_type}.",
        payload={"status": attempt.status, "attempt_number": attempt.attempt_number},
    )
    emit_workflow_operation_log(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        event_type="workflow_operation_attempt_completed",
        message=summary or f"Completed {operation.operation_type}.",
        metadata={"status": attempt.status},
    )


def fail_workflow_operation(
    session: Session,
    *,
    operation: WorkflowOperation,
    attempt: WorkflowOperationAttempt,
    category: str,
    message: str,
    next_retry_at: datetime | None = None,
) -> None:
    now = _now()
    retryable = True
    scheduled_for_retry = next_retry_at is not None
    operation.status = OPERATION_STATUS_RETRYING if scheduled_for_retry else OPERATION_STATUS_FAILED
    operation.summary = message
    operation.finished_at = None if scheduled_for_retry else now
    operation.updated_at = now
    attempt.status = OPERATION_STATUS_RETRYING if scheduled_for_retry else OPERATION_STATUS_FAILED
    attempt.error_category = category
    attempt.error_message = message
    attempt.status_detail = None
    attempt.retryable = retryable
    attempt.next_retry_at = next_retry_at
    attempt.lease_expires_at = None
    attempt.finished_at = now
    record_workflow_operation_audit_event(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        source_component="workflow_operation_service",
        event_kind="attempt_retry_scheduled" if scheduled_for_retry else "attempt_failed",
        level="warn" if scheduled_for_retry else "error",
        message=message,
        payload={
            "status": attempt.status,
            "attempt_number": attempt.attempt_number,
            "error_category": category,
            "retryable": retryable,
            "next_retry_at": next_retry_at.isoformat() if next_retry_at is not None else None,
        },
    )
    emit_workflow_operation_log(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        event_type="workflow_operation_attempt_retry_scheduled" if scheduled_for_retry else "workflow_operation_attempt_failed",
        message=message,
        level=logging.WARNING if scheduled_for_retry else logging.ERROR,
        metadata={
            "status": attempt.status,
            "error_category": category,
            "retryable": retryable,
            "next_retry_at": next_retry_at.isoformat() if next_retry_at is not None else None,
        },
    )


def mark_workflow_operation_waiting_for_input(
    session: Session,
    *,
    operation: WorkflowOperation,
    attempt: WorkflowOperationAttempt,
    summary: str,
) -> None:
    now = _now()
    operation.status = OPERATION_STATUS_WAITING_FOR_INPUT
    operation.summary = summary
    operation.finished_at = None
    operation.updated_at = now
    attempt.status = OPERATION_STATUS_WAITING_FOR_INPUT
    attempt.error_category = None
    attempt.error_message = None
    attempt.status_detail = summary
    attempt.retryable = False
    attempt.next_retry_at = None
    attempt.lease_expires_at = None
    attempt.finished_at = now
    record_workflow_operation_audit_event(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        source_component="workflow_operation_service",
        event_kind="waiting_for_input",
        level="info",
        message=summary,
        payload={"status": attempt.status, "attempt_number": attempt.attempt_number},
    )
    emit_workflow_operation_log(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation=operation, attempt=attempt),
        event_type="workflow_operation_attempt_waiting_for_input",
        message=summary,
        metadata={"status": attempt.status},
    )
