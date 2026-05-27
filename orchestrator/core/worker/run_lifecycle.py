from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.orm import Session

from orchestrator.core.workflow.execution_lifecycle import (
    apply_execution_for_dispatch_claim,
    apply_execution_for_run_started,
    apply_execution_for_run_terminal,
)
from orchestrator.core.projects.routing import find_active_project_for_issue_key
from orchestrator.core.runs.service import mark_run_terminal
from orchestrator.core.workflow.checkpoints import (
    checkpoint_kind_for_stage,
    upsert_workflow_checkpoint,
)
from orchestrator.core.workflow.execution_artifacts import (
    MissingDurableExecutionArtifactError,
    latest_pushed_execution_artifact_for_run,
    snapshot_requires_durable_execution_artifact,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.execution_snapshot import SnapshotWorkflow
from orchestrator.core.worker.stage_events import WorkerStageUpdate
from orchestrator.core.workflow.runner import WorkflowResult, WorkflowStageCheckpoint
from orchestrator.core.worker.run_disposition import resolve_run_disposition
from orchestrator.core.worker.run_transition_service import RunOwnership
from orchestrator.core.worker.run_transition_service import WorkerRunTransitionService
from orchestrator.storage.models import Project, Run, WorkflowExecution

RUN_STATUS_DISPATCHING = "dispatching"
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
    expected_claim_id: str | None = None,
) -> Run | None:
    started_at = datetime.now(timezone.utc)
    normalized_claim_id = str(expected_claim_id or "").strip() or None
    if expected_status is None:
        run.status = RUN_STATUS_RUNNING
        run.dispatch_claimed_at = None
        run.started_at = started_at
        run.last_heartbeat_at = started_at
        run.worker_service_instance_id = str(worker_service_instance_id or "").strip() or None
        apply_execution_for_run_started(
            session=session,
            run=run,
            now=started_at,
        )
        session.commit()
        session.refresh(run)
        return run

    result = session.execute(
        update(Run)
        .where(
            Run.run_id == run.run_id,
            Run.status == expected_status,
            *(
                [Run.worker_service_instance_id == str(worker_service_instance_id or "").strip()]
                if normalized_claim_id is not None and str(worker_service_instance_id or "").strip()
                else []
            ),
            *([Run.claim_id == normalized_claim_id] if normalized_claim_id is not None else []),
        )
        .values(
            status=RUN_STATUS_RUNNING,
            dispatch_claimed_at=None,
            started_at=started_at,
            last_heartbeat_at=started_at,
            worker_service_instance_id=str(worker_service_instance_id or "").strip() or None,
        )
    )
    if int(result.rowcount or 0) == 0:
        session.rollback()
        return None
    run.status = RUN_STATUS_RUNNING
    run.dispatch_claimed_at = None
    run.started_at = started_at
    run.last_heartbeat_at = started_at
    run.worker_service_instance_id = str(worker_service_instance_id or "").strip() or None
    apply_execution_for_run_started(
        session=session,
        run=run,
        now=started_at,
    )
    session.commit()
    session.refresh(run)
    return run


def claim_run_for_dispatch(
    session: Session,
    *,
    run: Run,
    expected_status: str,
    worker_service_instance_id: str | None = None,
    claim_id: str | None = None,
) -> Run | None:
    claimed_at = datetime.now(timezone.utc)
    normalized_owner = str(worker_service_instance_id or "").strip() or None
    normalized_claim_id = str(claim_id or "").strip() or uuid4().hex
    result = session.execute(
        update(Run)
        .where(
            Run.run_id == run.run_id,
            Run.status == expected_status,
        )
        .values(
            status=RUN_STATUS_DISPATCHING,
            claim_id=normalized_claim_id,
            dispatch_claimed_at=claimed_at,
            last_heartbeat_at=None,
            worker_service_instance_id=normalized_owner,
        )
    )
    if int(result.rowcount or 0) == 0:
        session.rollback()
        return None
    run.status = RUN_STATUS_DISPATCHING
    run.claim_id = normalized_claim_id
    run.dispatch_claimed_at = claimed_at
    run.last_heartbeat_at = None
    run.worker_service_instance_id = normalized_owner
    apply_execution_for_dispatch_claim(
        session=session,
        run=run,
        now=claimed_at,
    )
    session.commit()
    session.refresh(run)
    return run


def promote_run_to_running(
    session: Session,
    *,
    run: Run,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run | None:
    ownership = RunOwnership.from_expected(
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )
    WorkerRunTransitionService(session=session).require_owned_run(
        run=run,
        ownership=ownership,
        allow_statuses={RUN_STATUS_DISPATCHING},
        action="promote_run_to_running",
    )
    return start_run(
        session,
        run=run,
        expected_status=RUN_STATUS_DISPATCHING,
        worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )


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


def fail_guardrail_violation(
    session: Session,
    *,
    run: Run,
    error: str,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    return mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=RUN_STATUS_FAILED,
        last_error=f"Guardrail policy violation: {error}",
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )


def fail_project_repository_checkout(
    session: Session,
    *,
    run: Run,
    error: str,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    return mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=RUN_STATUS_FAILED,
        last_error=f"Project repository checkout failed: {error}",
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )


def fail_project_repository_setup(
    session: Session,
    *,
    run: Run,
    error: str,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    return mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=RUN_STATUS_FAILED,
        last_error=f"Project repository setup failed: {error}",
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )


def _normalize_stage_updates(
    stage_updates: list[WorkerStageUpdate | dict[str, str]],
) -> list[dict[str, str]]:
    payloads: list[dict[str, str]] = []
    for item in stage_updates:
        normalized = item if isinstance(item, WorkerStageUpdate) else WorkerStageUpdate.load(item)
        if normalized is None:
            continue
        payloads.append(normalized.to_payload())
    return payloads


def finalize_cancelled_run(
    session: Session,
    *,
    run: Run,
    stage_updates: list[WorkerStageUpdate | dict[str, str]],
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    ownership = RunOwnership.from_expected(
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )
    transitions = WorkerRunTransitionService(session=session)
    run = transitions.require_owned_run(
        run=run,
        ownership=ownership,
        allow_statuses={run.status},
        action="finalize_cancelled_run",
    )
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.workflow = SnapshotWorkflow(
        outcome="blocked",
        attempts=max(snapshot.workflow.attempts, 0),
        summary=["Run cancelled during execution"],
        blocker_message="Run cancelled during execution",
        requeue_target=None,
        requeue_reason=None,
    )
    snapshot.events.stage_updates = _normalize_stage_updates(stage_updates)
    run.plan = snapshot.dump()
    if run.finished_at is None:
        run.finished_at = datetime.now(timezone.utc)
    transitions.release_claim(run)
    apply_execution_for_run_terminal(
        session=session,
        run=run,
        now=run.finished_at or datetime.now(timezone.utc),
    )
    session.commit()
    session.refresh(run)
    return run


def finalize_workflow_result(
    session: Session,
    *,
    run: Run,
    workflow_result: WorkflowResult,
    stage_updates: list[WorkerStageUpdate | dict[str, str]],
    execution_context: dict[str, str] | None = None,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    ownership = RunOwnership.from_expected(
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )
    transitions = WorkerRunTransitionService(session=session)
    run = transitions.require_owned_run(
        run=run,
        ownership=ownership,
        allow_statuses={RUN_STATUS_RUNNING},
        action="finalize_workflow_result",
    )
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.apply_execution_context(execution_context)
    snapshot.apply_workflow_result(
        workflow_result=workflow_result,
        stage_updates=stage_updates,
    )
    run.plan = snapshot.dump()
    run.pr_url = workflow_result.pr_url
    run.finished_at = datetime.now(timezone.utc)
    transitions.release_claim(run)
    disposition = resolve_run_disposition(workflow_result=workflow_result)
    run.status = disposition.status
    run.last_error = disposition.last_error

    apply_execution_for_run_terminal(
        session=session,
        run=run,
        now=run.finished_at or datetime.now(timezone.utc),
    )
    session.commit()
    session.refresh(run)
    return run


def requeue_workflow_result_for_capability(
    session: Session,
    *,
    run: Run,
    workflow_result: WorkflowResult,
    stage_updates: list[WorkerStageUpdate | dict[str, str]],
    required_worker_capability: str,
    required_worker_label: str,
    execution_context: dict[str, str] | None = None,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    ownership = RunOwnership.from_expected(
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )
    transitions = WorkerRunTransitionService(session=session)
    run = transitions.require_owned_run(
        run=run,
        ownership=ownership,
        allow_statuses={RUN_STATUS_RUNNING},
        action="requeue_workflow_result_for_capability",
    )
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.apply_execution_context(execution_context)
    snapshot.apply_workflow_result(
        workflow_result=workflow_result,
        stage_updates=stage_updates,
    )
    snapshot.workflow.requeue_target = required_worker_capability
    snapshot.context.execution_context["required_worker_label"] = required_worker_label
    run.plan = snapshot.dump()
    run.required_worker_capability = required_worker_capability
    transitions.reset_for_new_attempt(run=run, now=datetime.now(timezone.utc))
    session.commit()
    session.refresh(run)
    return run


def requeue_workflow_result_for_stale_snapshot(
    session: Session,
    *,
    run: Run,
    workflow_result: WorkflowResult,
    stage_updates: list[WorkerStageUpdate | dict[str, str]],
    error: str,
    execution_context: dict[str, str] | None = None,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    ownership = RunOwnership.from_expected(
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )
    transitions = WorkerRunTransitionService(session=session)
    run = transitions.require_owned_run(
        run=run,
        ownership=ownership,
        allow_statuses={RUN_STATUS_RUNNING},
        action="requeue_workflow_result_for_stale_snapshot",
    )
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
    transitions.reset_for_new_attempt(run=run, now=datetime.now(timezone.utc))
    session.commit()
    session.refresh(run)
    return run


def requeue_run_for_repo_setup(
    session: Session,
    *,
    run: Run,
    stage_updates: list[dict[str, str]],
    error: str,
    expected_worker_service_instance_id: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    ownership = RunOwnership.from_expected(
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )
    transitions = WorkerRunTransitionService(session=session)
    run = transitions.require_owned_run(
        run=run,
        ownership=ownership,
        allow_statuses={RUN_STATUS_RUNNING},
        action="requeue_run_for_repo_setup",
    )
    snapshot = _load_or_init_snapshot(run.plan)
    attempts = snapshot.context.execution_context.get("repo_setup_attempts")
    try:
        repo_setup_attempts = max(0, int(attempts)) + 1
    except (TypeError, ValueError):
        repo_setup_attempts = 1
    snapshot.context.execution_context["repo_setup_attempts"] = repo_setup_attempts
    snapshot.context.execution_context["repo_setup_last_error"] = error
    snapshot.workflow.outcome = "requeue"
    snapshot.workflow.requeue_target = None
    snapshot.workflow.requeue_reason = error
    snapshot.events.stage_updates = _normalize_stage_updates(stage_updates)
    run.plan = snapshot.dump()
    run.pr_url = None
    transitions.reset_for_new_attempt(run=run, now=datetime.now(timezone.utc))
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
    expected_claim_id: str | None = None,
) -> Run:
    ownership = RunOwnership.from_expected(
        expected_worker_service_instance_id=expected_worker_service_instance_id,
        expected_claim_id=expected_claim_id,
    )
    run = WorkerRunTransitionService(session=session).require_owned_run(
        run=run,
        ownership=ownership,
        allow_statuses={RUN_STATUS_RUNNING},
        action="persist_stage_checkpoint",
    )
    snapshot = _load_or_init_snapshot(run.plan)
    snapshot.apply_stage_checkpoint(checkpoint)
    snapshot.apply_execution_context(execution_context)
    if snapshot_requires_durable_execution_artifact(snapshot.dump()) and latest_pushed_execution_artifact_for_run(
        session=session,
        run_id=run.run_id,
    ) is None:
        raise MissingDurableExecutionArtifactError(
            f"{checkpoint.stage.upper()} checkpoint is not reusable until the execution branch is pushed."
        )
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
