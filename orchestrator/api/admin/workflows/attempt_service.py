from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from orchestrator.api.admin.workflows.execution_state_read_model import resolve_resume_execution_state
from orchestrator.api.admin.workflows.queries import (
    latest_checkpoint_for_kind,
    latest_decision_issue_labels_for_workflow,
    latest_resumable_checkpoint,
    latest_run_for_workflow,
    pending_input_request,
    workflow_by_execution_id,
    workflow_checkpoint_kinds,
)
from orchestrator.core.config import get_settings
from orchestrator.core.runs.service import (
    RunBootstrap,
    RunStateTransitionError,
    require_ready_for_agent_enqueue,
    resolve_enqueue_precheck_outcome,
    resolve_pr_url_for_enqueue,
    resolve_required_worker_capability_from_plan,
)
from orchestrator.core.runtime.requirements import resolve_required_runtime_kinds_for_workflow
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.execution_artifacts import (
    MissingDurableExecutionArtifactError,
    attach_artifact_to_resume_plan,
    require_durable_execution_artifact_for_checkpoint,
)
from orchestrator.core.workflow.transitions import ATTEMPT_ENTRY_MODES, attempt_creation_policy
from orchestrator.core.workflow.attempt_factory import build_workflow_execution_for_attempt
from orchestrator.core.workflow.execution_lifecycle import reconcile_execution_with_active_run_state
from orchestrator.core.workflow.runtime import build_workflow_runtime
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.core.worker.capabilities import infer_required_worker_capability
from orchestrator.storage.models import DecisionCase, Project, Run, RunHumanInputRequest, WorkflowCheckpoint


def now() -> datetime:
    return datetime.now(timezone.utc)


def reconcile_workflow_status_with_active_attempt(*, session, workflow) -> None:  # noqa: ANN001
    active_run_id = str(getattr(workflow, "active_run_id", "") or "").strip()
    if not active_run_id:
        return
    active_run = session.get(Run, active_run_id)
    if active_run is None:
        return
    reconcile_execution_with_active_run_state(
        workflow=workflow,
        active_run=active_run,
        now=now(),
    )


def resolve_project(*, session, workflow, selected_checkpoint) -> Project | None:  # noqa: ANN001
    project_id = str(workflow.project_id or "").strip() or None
    if project_id:
        project = session.get(Project, project_id)
        if project is not None:
            return project
    if selected_checkpoint.run_id:
        parent_run = session.get(Run, selected_checkpoint.run_id)
        parent_project_id = str(getattr(parent_run, "project_id", "") or "").strip() or None
        if parent_project_id:
            return session.get(Project, parent_project_id)
    return None


def resolve_project_for_fresh_start(*, session, workflow, source_run) -> Project | None:  # noqa: ANN001
    project_id = str(workflow.project_id or "").strip() or None
    if project_id:
        project = session.get(Project, project_id)
        if project is not None:
            return project
    source_project_id = str(getattr(source_run, "project_id", "") or "").strip() or None
    if source_project_id:
        return session.get(Project, source_project_id)
    return None


def fresh_start_plan(*, source_run: Run | None) -> dict[str, object] | None:
    if source_run is None or source_run.plan is None:
        return None
    source_snapshot = ExecutionSnapshot.require(source_run.plan, allow_empty=False)
    next_snapshot = ExecutionSnapshot.empty(trigger_context=source_snapshot.context.trigger_context)
    precheck_outcome = (
        str(getattr(source_run, "pre_check_outcome", "") or "").strip()
        or str(source_snapshot.context.execution_context.get("pre_check_outcome") or "").strip()
    )
    if precheck_outcome:
        next_snapshot.context.execution_context["pre_check_outcome"] = precheck_outcome
    return next_snapshot.dump()


def checkpoint_resume_plan(*, checkpoint: WorkflowCheckpoint) -> dict[str, object]:
    return ExecutionSnapshot.require(
        checkpoint.payload_json,
        allow_empty=False,
    ).dump()


def durable_checkpoint_resume_plan(*, session, checkpoint: WorkflowCheckpoint) -> dict[str, object]:  # noqa: ANN001
    artifact = require_durable_execution_artifact_for_checkpoint(session=session, checkpoint=checkpoint)
    return attach_artifact_to_resume_plan(
        plan=checkpoint_resume_plan(checkpoint=checkpoint),
        artifact=artifact,
    )


def resolve_precheck_outcome_for_admin_attempt(*, source_run: Run | None, plan: object | None) -> str | None:
    source_precheck = None
    if source_run is not None:
        persisted = str(getattr(source_run, "pre_check_outcome", "") or "").strip()
        if persisted:
            source_precheck = persisted
    return resolve_enqueue_precheck_outcome(
        source="admin_workflow_attempt",
        precheck_outcome=source_precheck,
        precheck_source_plan=plan,
    )


def resolve_required_worker_capability_for_admin_attempt(*, session, workflow, source_run: Run | None, plan: object | None) -> str | None:  # noqa: ANN001
    if source_run is not None:
        persisted = str(getattr(source_run, "required_worker_capability", "") or "").strip()
        if persisted:
            return persisted
    plan_capability = resolve_required_worker_capability_from_plan(plan)
    if plan_capability:
        return plan_capability
    case = session.execute(
        select(DecisionCase)
        .where(
            DecisionCase.tenant_id == workflow.tenant_id,
            DecisionCase.issue_key == workflow.source_ref,
        )
        .limit(1)
    ).scalar_one_or_none()
    case_capability = str(getattr(case, "required_worker_capability", "") or "").strip()
    if case_capability:
        return case_capability
    project_default_worker_capability = ""
    project_id = str(getattr(workflow, "project_id", "") or "").strip()
    if project_id:
        project = session.get(Project, project_id)
        if project is not None:
            project_default_worker_capability = str(
                (dict(getattr(project, "policy_overrides", {}) or {})).get("default_worker_capability") or ""
            ).strip()
    inferred = infer_required_worker_capability(
        issue_summary=workflow.display_name,
        issue_description=workflow.source_description,
        issue_labels=latest_decision_issue_labels_for_workflow(session=session, workflow=workflow),
        project_default_worker_capability=project_default_worker_capability,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        issue_key=workflow.source_ref,
    )
    return str(inferred or "").strip() or None


def resolve_pr_url_for_admin_attempt(*, workflow, source_run: Run | None, plan: object | None) -> str | None:  # noqa: ANN001
    source_pr_url = str(getattr(source_run, "pr_url", "") or "").strip() or None
    workflow_pr_url = str(getattr(workflow, "pr_url", "") or "").strip() or None
    return resolve_pr_url_for_enqueue(
        pr_url=source_pr_url or workflow_pr_url,
        pr_url_source_plan=plan,
    )


def require_ready_for_queue(*, source: str, plan: object | None, precheck_outcome: str | None) -> None:
    try:
        require_ready_for_agent_enqueue(
            source=source,
            precheck_outcome=precheck_outcome,
            precheck_source_plan=plan,
        )
    except RunStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def cancel_open_input_requests(*, session, workflow_id: str) -> None:  # noqa: ANN001
    open_requests = session.execute(
        select(RunHumanInputRequest).where(
            RunHumanInputRequest.workflow_id == workflow_id,
            RunHumanInputRequest.status.in_(("pending", "answered")),
        )
    ).scalars().all()
    for request in open_requests:
        request.status = "cancelled"


def is_active_scope_unique_violation(error: IntegrityError) -> bool:
    message = str(error).lower()
    if "uq_workflow_executions_active_scope" in message:
        return True
    return (
        "workflow_executions.tenant_id" in message
        and "workflow_executions.source_system" in message
        and "workflow_executions.source_ref" in message
        and "workflow_executions.dedupe_scope" in message
    )


def create_workflow_attempt(
    *,
    session,
    execution_id: str,
    mode: str,
    checkpoint_kind: str | None,
    tenant_model,
    run_to_schema_fn,
):  # noqa: ANN001
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    reconcile_workflow_status_with_active_attempt(session=session, workflow=workflow)
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ATTEMPT_ENTRY_MODES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid attempt mode")
    creation_policy = attempt_creation_policy(workflow_status=workflow.status, mode=normalized_mode)
    if not creation_policy.allowed:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow already has an active attempt")

    selected_checkpoint = None
    source_run = latest_run_for_workflow(session=session, workflow_id=workflow.workflow_id)
    if normalized_mode != "fresh":
        selected_checkpoint = latest_checkpoint_for_kind(
            session=session,
            workflow_id=workflow.workflow_id,
            checkpoint_kind=str(checkpoint_kind or "").strip(),
        )
        if selected_checkpoint is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No checkpoint is available for that kind")

    tenant = session.get(tenant_model, workflow.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found for workflow")

    project = (
        resolve_project_for_fresh_start(session=session, workflow=workflow, source_run=source_run)
        if normalized_mode == "fresh"
        else resolve_project(session=session, workflow=workflow, selected_checkpoint=selected_checkpoint)
    )
    if project is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active project mapping found for workflow")
    if bool(getattr(project, "is_archived", False)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Project {project.project_id} is archived")

    now_value = now()
    next_workflow = workflow
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    orchestration_backend = str(workflow_type.orchestration_backend).strip().lower()
    if not creation_policy.reuse_workflow:
        next_workflow = build_workflow_execution_for_attempt(
            workflow_id=str(uuid4()),
            workflow_type_key=workflow.workflow_type_key,
            tenant_id=workflow.tenant_id,
            project_id=project.project_id,
            source_system=workflow.source_system,
            source_ref=workflow.source_ref,
            display_name=workflow.display_name,
            source_description=workflow.source_description,
            repo_url=workflow.repo_url,
            branch=None if normalized_mode == "fresh" else workflow.branch,
            pr_url=None if normalized_mode == "fresh" else workflow.pr_url,
            orchestration_backend=orchestration_backend,
            dedupe_scope=workflow.dedupe_scope,
            status="pending",
            latest_checkpoint_id=selected_checkpoint.checkpoint_id if selected_checkpoint is not None else None,
            source_workflow_id=workflow.workflow_id,
            source_run_id=(
                source_run.run_id
                if normalized_mode == "fresh" and source_run is not None
                else selected_checkpoint.run_id
            ),
            created_at=now_value,
            updated_at=now_value,
        )
        session.add(next_workflow)
        session.flush()
    else:
        cancel_open_input_requests(session=session, workflow_id=workflow.workflow_id)
        next_workflow.latest_checkpoint_id = selected_checkpoint.checkpoint_id if selected_checkpoint is not None else None

    try:
        next_run_plan = (
            fresh_start_plan(source_run=source_run)
            if normalized_mode == "fresh"
            else durable_checkpoint_resume_plan(session=session, checkpoint=selected_checkpoint)
        )
    except MissingDurableExecutionArtifactError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Selected run/checkpoint has an unsupported execution snapshot shape",
        ) from exc
    next_run_precheck_outcome = resolve_precheck_outcome_for_admin_attempt(source_run=source_run, plan=next_run_plan)
    next_run_required_worker_capability = resolve_required_worker_capability_for_admin_attempt(
        session=session,
        workflow=next_workflow,
        source_run=source_run,
        plan=next_run_plan,
    )
    next_run_required_runtime_kinds = resolve_required_runtime_kinds_for_workflow(
        session=session,
        settings=get_settings(),
        tenant_id=next_workflow.tenant_id,
        project_id=project.project_id,
    )
    next_run_pr_url = resolve_pr_url_for_admin_attempt(workflow=next_workflow, source_run=source_run, plan=next_run_plan)
    next_workflow.pr_url = next_run_pr_url

    runtime = build_workflow_runtime(
        session=session,
        settings=get_settings(),
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
    )
    try:
        enqueue_result = runtime.create_attempt(
            workflow_id=next_workflow.workflow_id,
            bootstrap=RunBootstrap(
                workflow_id=next_workflow.workflow_id,
                parent_run_id=(
                    source_run.run_id
                    if normalized_mode == "fresh" and source_run is not None
                    else selected_checkpoint.run_id if selected_checkpoint is not None else None
                ),
                entry_mode=normalized_mode,
                entry_stage="orchestrated" if normalized_mode == "fresh" else selected_checkpoint.stage,
                entry_checkpoint_id=None if normalized_mode == "fresh" else selected_checkpoint.checkpoint_id,
                plan=next_run_plan,
                branch=next_workflow.branch,
                pr_url=next_run_pr_url,
                precheck_outcome=next_run_precheck_outcome,
                required_worker_capability=next_run_required_worker_capability,
                required_runtime_kinds_json=next_run_required_runtime_kinds,
            ),
            commit=False,
        )
    except IntegrityError as error:
        session.rollback()
        if is_active_scope_unique_violation(error):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A queued or in-progress workflow already exists for this issue "
                    "and dedupe scope. Resume the active workflow instead."
                ),
            ) from error
        raise
    except RunStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not enqueue_result.enqueued:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Workflow already has an active attempt")
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if is_active_scope_unique_violation(error):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A queued or in-progress workflow already exists for this issue "
                    "and dedupe scope. Resume the active workflow instead."
                ),
            ) from error
        raise
    session.refresh(enqueue_result.run)
    return run_to_schema_fn(enqueue_result.run, workflow_execution_id=workflow.execution_id)


def resume_workflow_execution(
    *,
    session,
    execution_id: str,
    tenant_model,
    run_to_schema_fn,
):  # noqa: ANN001
    workflow = workflow_by_execution_id(session=session, execution_id=execution_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    reconcile_workflow_status_with_active_attempt(session=session, workflow=workflow)
    pending_request = pending_input_request(session=session, workflow_id=workflow.workflow_id)
    checkpoint_kinds = workflow_checkpoint_kinds(session=session, workflow_id=workflow.workflow_id)
    can_resume, resume_unavailable_reason = resolve_resume_execution_state(
        workflow=workflow,
        checkpoint_kinds=checkpoint_kinds,
    )
    if not can_resume:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=resume_unavailable_reason or "Execution is not resumable",
        )
    if pending_request is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Waiting for human input.")

    selected_checkpoint = latest_resumable_checkpoint(session=session, workflow_id=workflow.workflow_id)
    if selected_checkpoint is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No resumable execution state is available.")

    tenant = session.get(tenant_model, workflow.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found for workflow")

    project = resolve_project(session=session, workflow=workflow, selected_checkpoint=selected_checkpoint)
    if project is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active project mapping found for workflow")
    if bool(getattr(project, "is_archived", False)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Project {project.project_id} is archived")

    source_run = latest_run_for_workflow(session=session, workflow_id=workflow.workflow_id)
    cancel_open_input_requests(session=session, workflow_id=workflow.workflow_id)
    workflow.latest_checkpoint_id = selected_checkpoint.checkpoint_id

    try:
        next_run_plan = durable_checkpoint_resume_plan(session=session, checkpoint=selected_checkpoint)
    except MissingDurableExecutionArtifactError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Selected run/checkpoint has an unsupported execution snapshot shape",
        ) from exc
    next_run_precheck_outcome = resolve_precheck_outcome_for_admin_attempt(source_run=source_run, plan=next_run_plan)
    next_run_required_worker_capability = resolve_required_worker_capability_for_admin_attempt(
        session=session,
        workflow=workflow,
        source_run=source_run,
        plan=next_run_plan,
    )
    next_run_required_runtime_kinds = resolve_required_runtime_kinds_for_workflow(
        session=session,
        settings=get_settings(),
        tenant_id=workflow.tenant_id,
        project_id=project.project_id,
    )
    next_run_pr_url = resolve_pr_url_for_admin_attempt(workflow=workflow, source_run=source_run, plan=next_run_plan)
    workflow.pr_url = next_run_pr_url

    runtime = build_workflow_runtime(
        session=session,
        settings=get_settings(),
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
    )
    try:
        enqueue_result = runtime.create_attempt(
            workflow_id=workflow.workflow_id,
            bootstrap=RunBootstrap(
                workflow_id=workflow.workflow_id,
                parent_run_id=selected_checkpoint.run_id,
                entry_mode="resume",
                entry_stage=selected_checkpoint.stage,
                entry_checkpoint_id=selected_checkpoint.checkpoint_id,
                plan=next_run_plan,
                branch=workflow.branch,
                pr_url=next_run_pr_url,
                precheck_outcome=next_run_precheck_outcome,
                required_worker_capability=next_run_required_worker_capability,
                required_runtime_kinds_json=next_run_required_runtime_kinds,
            ),
            commit=False,
        )
    except IntegrityError as error:
        session.rollback()
        if is_active_scope_unique_violation(error):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution already has an active attempt.") from error
        raise
    except RunStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not enqueue_result.enqueued:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution already has an active attempt.")
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if is_active_scope_unique_violation(error):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execution already has an active attempt.") from error
        raise
    session.refresh(enqueue_result.run)
    return run_to_schema_fn(enqueue_result.run, workflow_execution_id=workflow.execution_id)
