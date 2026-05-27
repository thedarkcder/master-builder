from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import WorkflowOperationRestartRequest, WorkflowOperationRetryRead
from orchestrator.core.workflow.operation_service import OPERATION_STATUS_COMPLETED, OPERATION_STATUS_FAILED, OPERATION_STATUS_RUNNING
from orchestrator.core.workflow.transitions import TERMINAL_WORKFLOW_STATUSES
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt

logger = logging.getLogger(__name__)
_TERMINAL_WORKFLOW_STATUSES = frozenset({*TERMINAL_WORKFLOW_STATUSES, "completed"})


@dataclass(frozen=True)
class StaleWorkflowOperationAttemptRef:
    execution_id: str
    workflow_id: str
    operation_id: str
    operation_type: str
    attempt_id: str
    attempt_number: int


RestartWorkflowOperationFn = Callable[..., WorkflowOperationRetryRead]


def close_active_operation_attempts_for_terminal_workflows(
    *,
    session: Session,
    actor: str,
) -> int:
    rows = session.execute(
        select(WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt)
        .join(WorkflowOperation, WorkflowOperation.workflow_id == WorkflowExecution.workflow_id)
        .join(WorkflowOperationAttempt, WorkflowOperationAttempt.operation_id == WorkflowOperation.operation_id)
        .where(
            WorkflowExecution.status.in_(_TERMINAL_WORKFLOW_STATUSES),
            WorkflowOperationAttempt.status == OPERATION_STATUS_RUNNING,
        )
    ).all()
    now = datetime.now(timezone.utc)
    for workflow, operation, attempt in rows:
        workflow_status = str(workflow.status or "").strip().lower()
        operation.status = OPERATION_STATUS_COMPLETED if workflow_status in {"completed", "succeeded"} else OPERATION_STATUS_FAILED
        operation.finished_at = operation.finished_at or now
        operation.updated_at = now
        attempt.status = "failed"
        attempt.status_detail = (
            "Closed active workflow operation attempt because the workflow is already terminal. "
            f"Actor: {actor}."
        )
        attempt.retryable = False
        attempt.next_retry_at = None
        attempt.lease_expires_at = None
        attempt.finished_at = attempt.finished_at or now
    if rows:
        session.commit()
        logger.warning(
            "terminal_workflow_active_operation_attempts_closed actor=%s count=%s",
            actor,
            len(rows),
        )
    return len(rows)


def stale_running_workflow_operation_attempt_refs(
    *,
    session: Session,
    now: datetime | None = None,
    stale_timeout_seconds: int = 300,
    limit: int = 50,
) -> list[StaleWorkflowOperationAttemptRef]:
    anchor = now or datetime.now(timezone.utc)
    cutoff = anchor - timedelta(seconds=max(60, int(stale_timeout_seconds)))
    rows = session.execute(
        select(WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt)
        .join(WorkflowOperation, WorkflowOperation.workflow_id == WorkflowExecution.workflow_id)
        .join(WorkflowOperationAttempt, WorkflowOperationAttempt.operation_id == WorkflowOperation.operation_id)
        .where(
            WorkflowExecution.status.notin_(_TERMINAL_WORKFLOW_STATUSES),
            WorkflowOperationAttempt.status == OPERATION_STATUS_RUNNING,
            or_(
                WorkflowOperationAttempt.lease_expires_at <= anchor,
                WorkflowOperationAttempt.lease_expires_at.is_(None)
                & (WorkflowOperationAttempt.last_heartbeat_at.is_not(None))
                & (WorkflowOperationAttempt.last_heartbeat_at <= cutoff),
                WorkflowOperationAttempt.lease_expires_at.is_(None)
                & (WorkflowOperationAttempt.last_heartbeat_at.is_(None))
                & (WorkflowOperationAttempt.started_at <= cutoff),
            ),
        )
        .order_by(WorkflowOperationAttempt.started_at.asc(), WorkflowOperationAttempt.attempt_number.asc())
        .limit(limit)
    ).all()
    return [
        StaleWorkflowOperationAttemptRef(
            execution_id=workflow.execution_id,
            workflow_id=workflow.workflow_id,
            operation_id=operation.operation_id,
            operation_type=operation.operation_type,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
        )
        for workflow, operation, attempt in rows
    ]


def recover_stale_workflow_operation_attempts(
    *,
    session_factory,
    stale_timeout_seconds: int,
    actor: str,
    restart_workflow_operation_fn: RestartWorkflowOperationFn,
    limit: int = 50,
) -> int:  # noqa: ANN001
    with session_factory() as session:
        close_active_operation_attempts_for_terminal_workflows(
            session=session,
            actor=actor,
        )
        stale_refs = stale_running_workflow_operation_attempt_refs(
            session=session,
            stale_timeout_seconds=stale_timeout_seconds,
            limit=limit,
        )

    recovered = 0
    for stale_ref in stale_refs:
        with session_factory() as session:
            try:
                restart_workflow_operation_fn(
                    session=session,
                    execution_id=stale_ref.execution_id,
                    operation_id=stale_ref.operation_id,
                    actor=actor,
                    payload=WorkflowOperationRestartRequest(
                        restart_reason=(
                            "Recovered stale running workflow operation attempt "
                            f"{stale_ref.attempt_number} ({stale_ref.attempt_id})."
                        )
                    ),
                )
            except HTTPException as exc:
                session.rollback()
                logger.warning(
                    "stale_workflow_operation_recovery_skipped execution_id=%s operation_id=%s attempt_id=%s "
                    "status_code=%s detail=%s",
                    stale_ref.execution_id,
                    stale_ref.operation_id,
                    stale_ref.attempt_id,
                    exc.status_code,
                    exc.detail,
                )
                continue
            recovered += 1
    return recovered
