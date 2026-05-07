from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import WorkflowOperationRestartRequest, WorkflowOperationRetryRead
from orchestrator.core.workflow.operation_service import OPERATION_STATUS_RUNNING
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt


@dataclass(frozen=True)
class StaleWorkflowOperationAttemptRef:
    execution_id: str
    workflow_id: str
    operation_id: str
    operation_type: str
    attempt_id: str
    attempt_number: int


RestartWorkflowOperationFn = Callable[..., WorkflowOperationRetryRead]


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
        stale_refs = stale_running_workflow_operation_attempt_refs(
            session=session,
            stale_timeout_seconds=stale_timeout_seconds,
            limit=limit,
        )

    recovered = 0
    for stale_ref in stale_refs:
        with session_factory() as session:
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
            recovered += 1
    return recovered
