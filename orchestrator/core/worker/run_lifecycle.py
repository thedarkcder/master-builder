from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import select
from sqlalchemy import update
from sqlalchemy.orm import Session

from orchestrator.core.project_routing import find_active_project_for_issue_key
from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.workflow.runner import WorkflowResult
from orchestrator.storage.models import Project, Run, RunLock, Tenant
from orchestrator.storage.run_queue_events import notify_run_enqueued

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"


def _with_preserved_trigger_context(*, current_plan: object | None, next_plan: dict) -> dict:
    if isinstance(next_plan.get("trigger_context"), dict):
        return next_plan
    if not isinstance(current_plan, dict):
        return next_plan
    trigger_context = current_plan.get("trigger_context")
    if not isinstance(trigger_context, dict):
        return next_plan
    merged = dict(next_plan)
    merged["trigger_context"] = dict(trigger_context)
    return merged


def _release_run_lock(session: Session, *, run: Run) -> None:
    session.execute(
        delete(RunLock).where(
            RunLock.tenant_id == run.tenant_id,
            RunLock.issue_key == run.issue_key,
            RunLock.run_id == run.run_id,
        )
    )


def _lock_tenant_row_for_claim(session: Session, *, tenant_id: str) -> None:
    tenant_row = select(Tenant.tenant_id).where(Tenant.tenant_id == tenant_id)
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        tenant_row = tenant_row.with_for_update()
    session.execute(tenant_row).scalar_one_or_none()


def _running_run_count_for_tenant(session: Session, *, tenant_id: str) -> int:
    return int(
        session.execute(
            select(func.count(Run.run_id)).where(
                Run.tenant_id == tenant_id,
                Run.status == RUN_STATUS_RUNNING,
            )
        ).scalar_one()
    )


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
        session.commit()
        session.refresh(run)
        return run

    if max_concurrent_runs is not None:
        max_allowed = max(1, int(max_concurrent_runs))
        _lock_tenant_row_for_claim(session, tenant_id=run.tenant_id)
        if _running_run_count_for_tenant(session, tenant_id=run.tenant_id) >= max_allowed:
            session.rollback()
            return None

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
    run.plan = _with_preserved_trigger_context(
        current_plan=run.plan,
        next_plan={
        "succeeded": False,
        "attempts": 0,
        "summary": ["Run cancelled during execution"],
        "test_guidance": [],
        "pr_url": run.pr_url,
        "stage_updates": stage_updates,
        },
    )
    if run.finished_at is None:
        run.finished_at = datetime.now(timezone.utc)
    _release_run_lock(session, run=run)
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
    plan_payload = workflow_result.to_plan_payload()
    plan_payload["stage_updates"] = stage_updates
    if execution_context:
        plan_payload["execution_context"] = execution_context
    run.plan = _with_preserved_trigger_context(current_plan=run.plan, next_plan=plan_payload)
    run.pr_url = workflow_result.pr_url
    run.finished_at = datetime.now(timezone.utc)
    if workflow_result.succeeded:
        run.status = RUN_STATUS_SUCCEEDED
        run.last_error = None
    else:
        run.status = RUN_STATUS_FAILED
        if workflow_result.diagnostics is not None:
            run.last_error = workflow_result.diagnostics.message
        else:
            run.last_error = "Workflow failed without diagnostics"

    _release_run_lock(session, run=run)
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
    plan_payload = workflow_result.to_plan_payload()
    plan_payload["stage_updates"] = stage_updates
    if execution_context:
        plan_payload["execution_context"] = execution_context
    plan_payload["required_worker_capability"] = required_worker_capability
    plan_payload["required_worker_label"] = required_worker_label
    plan_payload["requeued"] = True
    run.plan = _with_preserved_trigger_context(current_plan=run.plan, next_plan=plan_payload)
    run.status = "queued"
    run.last_error = None
    run.started_at = None
    run.last_heartbeat_at = None
    run.finished_at = None
    run.worker_service_instance_id = None
    notify_run_enqueued(
        session,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
    )
    _release_run_lock(session, run=run)
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
    plan_payload = workflow_result.to_plan_payload()
    plan_payload["stage_updates"] = stage_updates
    if execution_context:
        plan_payload["execution_context"] = execution_context
    plan_payload["requeued"] = True
    plan_payload["stale_branch_snapshot"] = True
    plan_payload["requeue_reason"] = error
    run.plan = _with_preserved_trigger_context(current_plan=run.plan, next_plan=plan_payload)
    run.pr_url = None
    run.status = "queued"
    run.last_error = None
    run.started_at = None
    run.last_heartbeat_at = None
    run.finished_at = None
    run.worker_service_instance_id = None
    notify_run_enqueued(
        session,
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
    )
    _release_run_lock(session, run=run)
    session.commit()
    session.refresh(run)
    return run
