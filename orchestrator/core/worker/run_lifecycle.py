from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy import update
from sqlalchemy.orm import Session

from orchestrator.core.project_routing import find_active_project_for_issue_key
from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.workflow.runner import WorkflowResult
from orchestrator.storage.models import Project, Run, RunLock

RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"


def _release_run_lock(session: Session, *, run: Run) -> None:
    session.execute(
        delete(RunLock).where(
            RunLock.tenant_id == run.tenant_id,
            RunLock.issue_key == run.issue_key,
            RunLock.run_id == run.run_id,
        )
    )


def start_run(session: Session, *, run: Run, expected_status: str | None = None) -> Run | None:
    started_at = datetime.now(timezone.utc)
    if expected_status is None:
        run.status = RUN_STATUS_RUNNING
        run.started_at = started_at
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
        )
    )
    if int(result.rowcount or 0) == 0:
        session.rollback()
        return None
    run.status = RUN_STATUS_RUNNING
    run.started_at = started_at
    session.commit()
    session.refresh(run)
    return run


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
) -> Run:
    run.plan = {
        "succeeded": False,
        "attempts": 0,
        "summary": ["Run cancelled during execution"],
        "test_guidance": [],
        "pr_url": run.pr_url,
        "stage_updates": stage_updates,
    }
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
) -> Run:
    plan_payload = workflow_result.to_plan_payload()
    plan_payload["stage_updates"] = stage_updates
    run.plan = plan_payload
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
) -> Run:
    plan_payload = workflow_result.to_plan_payload()
    plan_payload["stage_updates"] = stage_updates
    plan_payload["required_worker_capability"] = required_worker_capability
    plan_payload["required_worker_label"] = required_worker_label
    plan_payload["requeued"] = True
    run.plan = plan_payload
    run.status = "queued"
    run.last_error = None
    run.started_at = None
    run.finished_at = None
    _release_run_lock(session, run=run)
    session.commit()
    session.refresh(run)
    return run
