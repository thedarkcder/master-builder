from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.storage.models import Run, WorkflowExecution

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_DISPATCHING = "dispatching"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_FOR_INPUT = "waiting_for_input"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

_KEEP = object()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_run_status(raw_status: object | None) -> str:
    return str(raw_status or "").strip().lower()


def _workflow_status_for_run_status(run_status: str) -> str:
    normalized = _normalize_run_status(run_status)
    if normalized == RUN_STATUS_BLOCKED:
        return RUN_STATUS_FAILED
    return normalized


def _apply_projection(
    workflow: WorkflowExecution,
    *,
    now: datetime | None = None,
    status: object = _KEEP,
    last_error: object = _KEEP,
    active_run_id: object = _KEEP,
    latest_checkpoint_id: object = _KEEP,
    started_at: object = _KEEP,
    finished_at: object = _KEEP,
    orchestration_backend: object = _KEEP,
) -> WorkflowExecution:
    timestamp = now or _now()
    if status is not _KEEP:
        workflow.status = status
    if last_error is not _KEEP:
        workflow.last_error = last_error
    if active_run_id is not _KEEP:
        workflow.active_run_id = active_run_id
    if latest_checkpoint_id is not _KEEP:
        workflow.latest_checkpoint_id = latest_checkpoint_id
    if started_at is not _KEEP:
        workflow.started_at = started_at
    if finished_at is not _KEEP:
        workflow.finished_at = finished_at
    if orchestration_backend is not _KEEP:
        workflow.orchestration_backend = orchestration_backend
    workflow.updated_at = timestamp
    return workflow


def project_workflow_for_new_run_attempt(
    workflow: WorkflowExecution,
    *,
    run: Run,
    latest_checkpoint_id: str | None = None,
    orchestration_backend: str | None = None,
    now: datetime | None = None,
) -> WorkflowExecution:
    return _apply_projection(
        workflow,
        now=now,
        status=RUN_STATUS_QUEUED,
        last_error=None,
        active_run_id=run.run_id,
        latest_checkpoint_id=latest_checkpoint_id,
        finished_at=None,
        orchestration_backend=orchestration_backend if orchestration_backend is not None else _KEEP,
    )


def project_workflow_for_dispatch_claim(
    workflow: WorkflowExecution,
    *,
    run: Run,
    now: datetime | None = None,
) -> WorkflowExecution:
    return _apply_projection(
        workflow,
        now=now,
        active_run_id=run.run_id,
    )


def project_workflow_for_run_started(
    workflow: WorkflowExecution,
    *,
    run: Run,
    now: datetime | None = None,
) -> WorkflowExecution:
    timestamp = now or _now()
    return _apply_projection(
        workflow,
        now=timestamp,
        status=RUN_STATUS_RUNNING,
        active_run_id=run.run_id,
        started_at=workflow.started_at or timestamp,
    )


def project_workflow_for_waiting_input(
    workflow: WorkflowExecution,
    *,
    run: Run,
    latest_checkpoint_id: str | None = None,
    now: datetime | None = None,
) -> WorkflowExecution:
    return _apply_projection(
        workflow,
        now=now,
        status=RUN_STATUS_WAITING_FOR_INPUT,
        last_error=None,
        active_run_id=run.run_id,
        latest_checkpoint_id=latest_checkpoint_id,
        finished_at=None,
    )


def project_workflow_for_run_terminal(
    workflow: WorkflowExecution,
    *,
    run: Run,
    now: datetime | None = None,
) -> WorkflowExecution:
    timestamp = now or _now()
    workflow_status = _workflow_status_for_run_status(run.status)
    last_error = run.last_error if workflow_status == RUN_STATUS_FAILED else None
    return _apply_projection(
        workflow,
        now=timestamp,
        status=workflow_status,
        last_error=last_error,
        active_run_id=run.run_id,
        finished_at=run.finished_at or timestamp,
    )


def project_workflow_for_cancelled_attempt(
    workflow: WorkflowExecution,
    *,
    run: Run,
    cancellation_reason: str,
    remaining_active_runs: int,
    now: datetime | None = None,
) -> WorkflowExecution:
    timestamp = now or _now()
    active_run_id = workflow.active_run_id
    if str(active_run_id or "").strip() == str(run.run_id or "").strip():
        active_run_id = None
    status = RUN_STATUS_CANCELLED if remaining_active_runs <= 0 else _KEEP
    finished_at = timestamp if remaining_active_runs <= 0 else _KEEP
    return _apply_projection(
        workflow,
        now=timestamp,
        status=status,
        last_error=cancellation_reason,
        active_run_id=active_run_id,
        finished_at=finished_at,
    )


def reconcile_workflow_with_active_run(
    workflow: WorkflowExecution,
    *,
    active_run: Run,
    now: datetime | None = None,
) -> WorkflowExecution:
    normalized_status = _normalize_run_status(active_run.status)
    if normalized_status in {RUN_STATUS_QUEUED, RUN_STATUS_DISPATCHING, RUN_STATUS_RUNNING}:
        return workflow
    if normalized_status == RUN_STATUS_WAITING_FOR_INPUT:
        return _apply_projection(
            workflow,
            now=now,
            status=RUN_STATUS_WAITING_FOR_INPUT,
            last_error=None,
            finished_at=None,
        )
    if normalized_status in {
        RUN_STATUS_BLOCKED,
        RUN_STATUS_SUCCEEDED,
        RUN_STATUS_FAILED,
        RUN_STATUS_CANCELLED,
    }:
        return project_workflow_for_run_terminal(workflow, run=active_run, now=now)
    return workflow
