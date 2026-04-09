from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from orchestrator.core.project_routing import find_active_project_for_issue_key
from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.workflow.checkpoints import (
    checkpoint_kind_for_stage,
    upsert_workflow_checkpoint,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.execution_snapshot import SnapshotWorkflow
from orchestrator.core.workflow.runner import WorkflowResult, WorkflowStageCheckpoint
from orchestrator.storage.models import Project, Run, WorkflowExecution
from orchestrator.storage.run_queue_events import notify_run_enqueued

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"


def _load_or_init_snapshot(plan: object | None) -> ExecutionSnapshot:
    return ExecutionSnapshot.require(plan, allow_empty=True)


def _workflow_for_run(session: Session, *, run: Run) -> WorkflowExecution | None:
    return session.get(WorkflowExecution, run.workflow_id)


def start_run(
    session: Session,
    *,
    run: Run,
    expected_status: str | None = None,
    max_concurrent_runs: int | None = None,
    worker_service_instance_id: str | None = None,
) -> Run | None:
    started_at = datetime.now(timezone.utc)
    if expected_status is None:
        run.status = RUN_STATUS_RUNNING
        run.started_at = started_at
        run.last_heartbeat_at = started_at
        run.worker_service_instance_id = str(worker_service_instance_id or "").strip() or None
        workflow = _workflow_for_run(session, run=run)
        if workflow is not None:
            workflow.status = RUN_STATUS_RUNNING
            workflow.started_at = workflow.started_at or started_at
            workflow.active_run_id = run.run_id
            workflow.updated_at = started_at
        session.commit()
        session.refresh(run)
        return run

    result = session.execute(
        update(Run)
        .where(
            Run.run_id == run.run_id,
            Run.status == expected_status,
        )
        .values(
            status=RUN_STATUS_RUNNING,
            started_at=started_at,
            last_heartbeat_at=started_at,
            worker_service_instance_id=str(worker_service_instance_id or "").strip() or None,
        )
    )
    if int(result.rowcount or 0) == 0:
        session.rollback()
        return None
    run.status = RUN_STATUS_RUNNING
    run.started_at = started_at
    run.last_heartbeat_at = started_at
    run.worker_service_instance_id = str(worker_service_instance_id or "").strip() or None
    workflow = _workflow_for_run(session, run=run)
    if workflow is not None:
        workflow.status = RUN_STATUS_RUNNING
        workflow.started_at = workflow.started_at or started_at
        workflow.active_run_id = run.run_id
        workflow.updated_at = started_at
    session.commit()
    session.refresh(run)
    return run


def _refresh_owned_run(
    session: Session,
    *,
    run: Run,
    expected_worker_service_instance_id: str | None,
    allow_statuses: set[str],
) -> Run:
    session.refresh(run)
    expected_owner = str(expected_worker_service_instance_id or "").strip()
    if not expected_owner:
        return run
    if run.status not in allow_statuses:
        return run
    current_owner = str(run.worker_service_instance_id or "").strip()
    if current_owner != expected_owner:
        return run
    return run


def _run_is_owned_by(
    *,
    run: Run,
    expected_worker_service_instance_id: str | None,
    allow_statuses: set[str],
) -> bool:
    expected_owner = str(expected_worker_service_instance_id or "").strip()
    if not expected_owner:
        return True
    if run.status not in allow_statuses:
        return False
    return str(run.worker_service_instance_id or "").strip() == expected_owner


def resolve_project_for_run(session: Session, *, run: Run) -> Project | None:
    project: Project | None = None
    if run.project_id:
        project = session.get(Project, run.project_id)
        if project is not None and project.tenant_id != run.tenant_id:
            project = None
    if project is None:
        project = find_active_project_for_issue_key(
            session,
            tenant_id=run.tenant_id,
            issue_key=run.issue_key,
        )
    return project


def fail_missing_project_mapping(session: Session, *, run: Run) -> Run:
    return mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=RUN_STATUS_FAILED,
        last_error=f"No active project mapping found for issue {run.issue_key}",
    )


def block_archived_project(session: Session, *, run: Run, project: Project) -> Run:
    return mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=RUN_STATUS_BLOCKED,
        last_error=f"Project {project.project_id} is archived; run blocked",
    )


def bind_run_project(session: Session, *, run: Run, project: Project) -> Run:
    if run.project_id != project.project_id:
        run.project_id = project.project_id
    if not run.repo_url:
        run.repo_url = project.github_repository
    session.commit()
    session.refresh(run)
    return run


def fail_guardrail_violation(session: Session, *, run: Run, error: str) -> Run:
    return mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=RUN_STATUS_FAILED,
        last_error=f"Guardrail policy violation: {error}",
    )


def fail_project_repository_checkout(session: Session, *, run: Run, error: str) -> Run:
    return mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=RUN_STATUS_FAILED,
        last_error=f"Project repository checkout failed: {error}",
    )


def finalize_cancelled_run(
    session: Session,
    *,
    run: Run,
    stage_updates: list[dict[str, str]],
    expected_worker_service_instance_id: str | None = None,
) -> Run:
    run = _refresh_owned_run(
        session,
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={run.status},
    )
    if not _run_is_owned_by(
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={run.status},
    ):
        return run
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.workflow = SnapshotWorkflow(
        outcome="blocked",
        attempts=max(snapshot.workflow.attempts, 0),
        summary=["Run cancelled during execution"],
        blocker_message="Run cancelled during execution",
        requeue_target=None,
        requeue_reason=None,
    )
    snapshot.events.stage_updates = [dict(item) for item in stage_updates if isinstance(item, dict)]
    run.plan = snapshot.dump()
    if run.finished_at is None:
        run.finished_at = datetime.now(timezone.utc)
    run.last_heartbeat_at = None
    run.worker_service_instance_id = None
    workflow = _workflow_for_run(session, run=run)
    if workflow is not None:
        workflow.status = run.status
        workflow.last_error = run.last_error
        workflow.active_run_id = run.run_id
        workflow.finished_at = run.finished_at
        workflow.updated_at = run.finished_at or datetime.now(timezone.utc)
    session.commit()
    session.refresh(run)
    return run


def finalize_workflow_result(
    session: Session,
    *,
    run: Run,
    workflow_result: WorkflowResult,
    stage_updates: list[dict[str, str]],
    execution_context: dict[str, str] | None = None,
    expected_worker_service_instance_id: str | None = None,
) -> Run:
    run = _refresh_owned_run(
        session,
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    )
    if not _run_is_owned_by(
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    ):
        return run
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.apply_execution_context(execution_context)
    snapshot.apply_workflow_result(
        workflow_result=workflow_result,
        stage_updates=stage_updates,
    )
    run.plan = snapshot.dump()
    run.pr_url = workflow_result.pr_url
    run.finished_at = datetime.now(timezone.utc)
    run.last_heartbeat_at = None
    run.worker_service_instance_id = None
    if workflow_result.outcome == "success":
        run.status = RUN_STATUS_SUCCEEDED
        run.last_error = None
    elif workflow_result.outcome == "failed":
        run.status = RUN_STATUS_FAILED
        run.last_error = (
            workflow_result.diagnostics.message
            if workflow_result.diagnostics is not None
            else (workflow_result.blocker_message or "Workflow failed without diagnostics")
        )
    else:
        run.status = RUN_STATUS_BLOCKED
        run.last_error = (
            workflow_result.blocker_message
            or (workflow_result.diagnostics.message if workflow_result.diagnostics is not None else None)
            or "Workflow blocked without diagnostics"
        )

    workflow = _workflow_for_run(session, run=run)
    if workflow is not None:
        workflow.status = run.status
        workflow.last_error = run.last_error
        workflow.active_run_id = run.run_id
        workflow.finished_at = None if run.status == RUN_STATUS_BLOCKED else run.finished_at
        workflow.updated_at = run.finished_at or datetime.now(timezone.utc)
        workflow.blocked_reason = run.last_error if run.status == RUN_STATUS_BLOCKED else None
    session.commit()
    session.refresh(run)
    return run


def requeue_workflow_result_for_capability(
    session: Session,
    *,
    run: Run,
    workflow_result: WorkflowResult,
    stage_updates: list[dict[str, str]],
    required_worker_capability: str,
    required_worker_label: str,
    execution_context: dict[str, str] | None = None,
    expected_worker_service_instance_id: str | None = None,
) -> Run:
    run = _refresh_owned_run(
        session,
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    )
    if not _run_is_owned_by(
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    ):
        return run
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.apply_execution_context(execution_context)
    snapshot.apply_workflow_result(
        workflow_result=workflow_result,
        stage_updates=stage_updates,
    )
    snapshot.workflow.requeue_target = required_worker_capability
    snapshot.context.execution_context["required_worker_label"] = required_worker_label
    run.plan = snapshot.dump()
    run.status = "queued"
    run.last_error = None
    run.started_at = None
    run.last_heartbeat_at = None
    run.finished_at = None
    run.worker_service_instance_id = None
    workflow = _workflow_for_run(session, run=run)
    if workflow is not None:
        workflow.status = "queued"
        workflow.last_error = None
        workflow.finished_at = None
        workflow.active_run_id = run.run_id
        workflow.updated_at = datetime.now(timezone.utc)
    notify_run_enqueued(
        session,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
    )
    session.commit()
    session.refresh(run)
    return run


def requeue_workflow_result_for_stale_snapshot(
    session: Session,
    *,
    run: Run,
    workflow_result: WorkflowResult,
    stage_updates: list[dict[str, str]],
    error: str,
    execution_context: dict[str, str] | None = None,
    expected_worker_service_instance_id: str | None = None,
) -> Run:
    run = _refresh_owned_run(
        session,
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    )
    if not _run_is_owned_by(
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    ):
        return run
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.apply_execution_context(execution_context)
    snapshot.apply_workflow_result(
        workflow_result=workflow_result,
        stage_updates=stage_updates,
    )
    snapshot.context.execution_context["stale_branch_snapshot"] = True
    snapshot.workflow.outcome = "requeue"
    snapshot.workflow.requeue_target = None
    snapshot.workflow.requeue_reason = error
    run.plan = snapshot.dump()
    run.pr_url = None
    run.status = "queued"
    run.last_error = None
    run.started_at = None
    run.last_heartbeat_at = None
    run.finished_at = None
    run.worker_service_instance_id = None
    workflow = _workflow_for_run(session, run=run)
    if workflow is not None:
        workflow.status = "queued"
        workflow.last_error = None
        workflow.finished_at = None
        workflow.active_run_id = run.run_id
        workflow.updated_at = datetime.now(timezone.utc)
    notify_run_enqueued(
        session,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
    )
    session.commit()
    session.refresh(run)
    return run


def persist_stage_checkpoint(
    session: Session,
    *,
    run: Run,
    checkpoint: WorkflowStageCheckpoint,
    execution_context: dict[str, str] | None = None,
    expected_worker_service_instance_id: str | None = None,
) -> Run:
    run = _refresh_owned_run(
        session,
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    )
    if not _run_is_owned_by(
        run=run,
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        allow_statuses={RUN_STATUS_RUNNING},
    ):
        raise RuntimeError("Run ownership lost while persisting stage checkpoint")
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.apply_stage_checkpoint(checkpoint)
    snapshot.apply_execution_context(execution_context)
    run.plan = snapshot.dump()
    if checkpoint.stage == "dev" and checkpoint.dev_result is not None:
        run.pr_url = checkpoint.dev_result.pr_url
    elif checkpoint.stage == "review" and checkpoint.review_result is not None:
        run.pr_url = checkpoint.review_result.pr_url or run.pr_url
    checkpoint_kind = checkpoint_kind_for_stage(checkpoint.stage)
    if checkpoint_kind is not None:
        upsert_workflow_checkpoint(
            session,
            workflow_id=run.workflow_id,
            run_id=run.run_id,
            checkpoint_kind=checkpoint_kind,
            stage=checkpoint.stage,
            payload=snapshot.dump(),
            now=datetime.now(timezone.utc),
        )
    session.commit()
    session.refresh(run)
    return run
