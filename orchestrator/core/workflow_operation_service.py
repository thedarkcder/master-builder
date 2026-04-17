from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.storage.models import WorkflowBlocker, WorkflowOperation, WorkflowOperationAttempt

OPERATION_STATUS_PENDING = "pending"
OPERATION_STATUS_RUNNING = "running"
OPERATION_STATUS_RETRYING = "retrying"
OPERATION_STATUS_BLOCKED = "blocked"
OPERATION_STATUS_FAILED = "failed"
OPERATION_STATUS_COMPLETED = "completed"

BLOCKER_STATUS_OPEN = "open"
BLOCKER_STATUS_RESOLVED = "resolved"


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        blocker_id=None,
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
    operation.updated_at = now
    attempt = WorkflowOperationAttempt(
        attempt_id=uuid4().hex,
        operation_id=operation.operation_id,
        attempt_number=next_attempt_number,
        status=OPERATION_STATUS_RUNNING,
        error_category=None,
        error_message=None,
        retryable=False,
        next_retry_at=None,
        created_at=now,
        started_at=now,
        finished_at=None,
    )
    session.add(attempt)
    session.flush()
    return attempt


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
    if operation.blocker_id:
        blocker = session.get(WorkflowBlocker, operation.blocker_id)
        if blocker is not None and blocker.status == BLOCKER_STATUS_OPEN:
            blocker.status = BLOCKER_STATUS_RESOLVED
            blocker.resolved_at = now
            blocker.updated_at = now
    attempt.status = OPERATION_STATUS_COMPLETED
    attempt.finished_at = now


def block_workflow_operation(
    session: Session,
    *,
    operation: WorkflowOperation,
    attempt: WorkflowOperationAttempt,
    category: str,
    message: str,
) -> WorkflowBlocker:
    now = _now()
    blocker = WorkflowBlocker(
        blocker_id=uuid4().hex,
        workflow_id=operation.workflow_id,
        operation_id=operation.operation_id,
        category=category,
        message=message,
        status=BLOCKER_STATUS_OPEN,
        created_at=now,
        resolved_at=None,
        updated_at=now,
    )
    session.add(blocker)
    session.flush()
    operation.status = OPERATION_STATUS_BLOCKED
    operation.blocker_id = blocker.blocker_id
    operation.summary = message
    operation.finished_at = now
    operation.updated_at = now
    attempt.status = OPERATION_STATUS_BLOCKED
    attempt.error_category = category
    attempt.error_message = message
    attempt.retryable = False
    attempt.finished_at = now
    return blocker


def fail_workflow_operation(
    session: Session,
    *,
    operation: WorkflowOperation,
    attempt: WorkflowOperationAttempt,
    category: str,
    message: str,
    retryable: bool,
    next_retry_at: datetime | None = None,
) -> None:
    now = _now()
    operation.status = OPERATION_STATUS_RETRYING if retryable else OPERATION_STATUS_FAILED
    operation.summary = message
    operation.finished_at = None if retryable else now
    operation.updated_at = now
    attempt.status = OPERATION_STATUS_RETRYING if retryable else OPERATION_STATUS_FAILED
    attempt.error_category = category
    attempt.error_message = message
    attempt.retryable = retryable
    attempt.next_retry_at = next_retry_at
    attempt.finished_at = now
