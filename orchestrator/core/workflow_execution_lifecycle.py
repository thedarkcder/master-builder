from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.workflow_execution_status import mark_workflow_failed
from orchestrator.core.workflow_run_state import (
    project_workflow_for_cancelled_attempt,
    project_workflow_for_dispatch_claim,
    project_workflow_for_new_run_attempt,
    project_workflow_for_run_started,
    project_workflow_for_run_terminal,
    project_workflow_for_waiting_input,
    reconcile_workflow_with_active_run,
)
from orchestrator.storage.models import Run, WorkflowExecution


def _now() -> datetime:
    return datetime.now(timezone.utc)


def workflow_for_run(*, session: Session, run: Run) -> WorkflowExecution | None:
    return session.get(WorkflowExecution, run.workflow_id)


def apply_execution_for_new_attempt(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution | None = None,
    latest_checkpoint_id: str | None = None,
    orchestration_backend: str | None = None,
    now: datetime | None = None,
) -> WorkflowExecution | None:
    workflow = workflow or workflow_for_run(session=session, run=run)
    if workflow is None:
        return None
    project_workflow_for_new_run_attempt(
        workflow,
        run=run,
        latest_checkpoint_id=latest_checkpoint_id,
        orchestration_backend=orchestration_backend,
        now=now,
    )
    return workflow


def apply_execution_for_dispatch_claim(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution | None = None,
    now: datetime | None = None,
) -> WorkflowExecution | None:
    workflow = workflow or workflow_for_run(session=session, run=run)
    if workflow is None:
        return None
    project_workflow_for_dispatch_claim(workflow, run=run, now=now)
    return workflow


def apply_execution_for_run_started(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution | None = None,
    now: datetime | None = None,
) -> WorkflowExecution | None:
    workflow = workflow or workflow_for_run(session=session, run=run)
    if workflow is None:
        return None
    project_workflow_for_run_started(workflow, run=run, now=now)
    return workflow


def apply_execution_for_waiting_input(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution | None = None,
    latest_checkpoint_id: str | None = None,
    now: datetime | None = None,
) -> WorkflowExecution | None:
    workflow = workflow or workflow_for_run(session=session, run=run)
    if workflow is None:
        return None
    project_workflow_for_waiting_input(
        workflow,
        run=run,
        latest_checkpoint_id=latest_checkpoint_id,
        now=now,
    )
    return workflow


def apply_execution_for_run_terminal(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution | None = None,
    now: datetime | None = None,
) -> WorkflowExecution | None:
    workflow = workflow or workflow_for_run(session=session, run=run)
    if workflow is None:
        return None
    project_workflow_for_run_terminal(workflow, run=run, now=now)
    return workflow


def apply_execution_for_cancelled_attempt(
    *,
    session: Session,
    run: Run,
    workflow: WorkflowExecution | None = None,
    cancellation_reason: str,
    remaining_active_runs: int,
    now: datetime | None = None,
) -> WorkflowExecution | None:
    workflow = workflow or workflow_for_run(session=session, run=run)
    if workflow is None:
        return None
    project_workflow_for_cancelled_attempt(
        workflow,
        run=run,
        cancellation_reason=cancellation_reason,
        remaining_active_runs=remaining_active_runs,
        now=now,
    )
    return workflow


def reconcile_execution_with_active_run_state(
    *,
    workflow: WorkflowExecution,
    active_run: Run,
    now: datetime | None = None,
) -> WorkflowExecution:
    reconcile_workflow_with_active_run(
        workflow,
        active_run=active_run,
        now=now,
    )
    return workflow


def apply_execution_failure(
    *,
    workflow: WorkflowExecution,
    message: str,
    now: datetime | None = None,
) -> WorkflowExecution:
    mark_workflow_failed(workflow=workflow, message=message, now=now or _now())
    workflow.active_run_id = None
    return workflow
