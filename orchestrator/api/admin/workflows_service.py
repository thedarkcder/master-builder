from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import desc, or_, select
from sqlalchemy.exc import IntegrityError

from orchestrator.core.worker_capabilities import infer_required_worker_capability
from orchestrator.core.runs import (
    RunStateTransitionError,
    require_ready_for_agent_enqueue,
    resolve_precheck_outcome_from_plan,
    resolve_required_worker_capability_from_plan,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.transitions import ATTEMPT_ENTRY_MODES, attempt_creation_policy
from orchestrator.storage.models import (
    DecisionCase,
    DecisionEvent,
    Project,
    Run,
    RunHumanInputRequest,
    WorkflowCheckpoint,
    WorkflowExecution,
)
from orchestrator.storage.run_queue_events import notify_run_enqueued


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reconcile_workflow_status_with_active_attempt(*, session, workflow) -> None:  # noqa: ANN001
    active_run_id = str(getattr(workflow, "active_run_id", "") or "").strip()
    if not active_run_id:
        return
    active_run = session.get(Run, active_run_id)
    if active_run is None:
        return
    run_status = str(getattr(active_run, "status", "") or "").strip().lower()
    if run_status in {"queued", "dispatching", "running"}:
        return

    now = _now()
    workflow.updated_at = now
    workflow.last_error = active_run.last_error
    if run_status == "waiting_for_input":
        workflow.status = "waiting_for_input"
        workflow.finished_at = None
        workflow.blocked_reason = None
        return
    if run_status == "blocked":
        workflow.status = "blocked"
        workflow.finished_at = None
        workflow.blocked_reason = active_run.last_error
        return
    if run_status in {"succeeded", "failed", "cancelled"}:
        workflow.status = run_status
        workflow.finished_at = active_run.finished_at or now
        workflow.blocked_reason = None


def _latest_checkpoint_for_kind(*, session, workflow_id: str, checkpoint_kind: str) -> WorkflowCheckpoint | None:  # noqa: ANN001
    return session.execute(
        select(WorkflowCheckpoint)
        .where(
            WorkflowCheckpoint.workflow_id == workflow_id,
            WorkflowCheckpoint.checkpoint_kind == checkpoint_kind,
        )
        .order_by(desc(WorkflowCheckpoint.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _pending_input_request_id(*, session, workflow_id: str) -> str | None:  # noqa: ANN001
    row = session.execute(
        select(RunHumanInputRequest.request_id)
        .where(
            RunHumanInputRequest.workflow_id == workflow_id,
            RunHumanInputRequest.status == "pending",
        )
        .order_by(desc(RunHumanInputRequest.created_at))
        .limit(1)
    ).first()
    return str(row[0]) if row else None


def _workflow_runs(*, session, workflow_id: str) -> list[Run]:  # noqa: ANN001
    return session.execute(
        select(Run)
        .where(Run.workflow_id == workflow_id)
        .order_by(Run.attempt_number.asc())
    ).scalars().all()


def _latest_run_for_workflow(*, session, workflow_id: str) -> Run | None:  # noqa: ANN001
    return session.execute(
        select(Run)
        .where(Run.workflow_id == workflow_id)
        .order_by(desc(Run.attempt_number))
        .limit(1)
    ).scalar_one_or_none()


def _workflow_schema(*, session, workflow, workflow_to_schema_fn, run_to_schema_fn):  # noqa: ANN001
    latest_checkpoint = (
        session.get(WorkflowCheckpoint, workflow.latest_checkpoint_id)
        if workflow.latest_checkpoint_id
        else None
    )
    return workflow_to_schema_fn(
        workflow,
        runs=[run_to_schema_fn(run) for run in _workflow_runs(session=session, workflow_id=workflow.workflow_id)],
        pending_input_request_id=_pending_input_request_id(session=session, workflow_id=workflow.workflow_id),
        latest_checkpoint_kind=latest_checkpoint.checkpoint_kind if latest_checkpoint is not None else None,
    )


def _resolve_project(*, session, workflow, selected_checkpoint) -> Project | None:  # noqa: ANN001
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


def _resolve_project_for_fresh_start(*, session, workflow, source_run) -> Project | None:  # noqa: ANN001
    project_id = str(workflow.project_id or "").strip() or None
    if project_id:
        project = session.get(Project, project_id)
        if project is not None:
            return project
    source_project_id = str(getattr(source_run, "project_id", "") or "").strip() or None
    if source_project_id:
        return session.get(Project, source_project_id)
    return None


def _fresh_start_plan(*, source_run: Run | None) -> dict[str, object] | None:
    if source_run is None or not isinstance(source_run.plan, dict):
        return None
    source_snapshot = ExecutionSnapshot.load(source_run.plan)
    if source_snapshot is None:
        return None
    next_snapshot = ExecutionSnapshot.empty(trigger_context=source_snapshot.context.trigger_context)
    precheck_outcome = (
        str(getattr(source_run, "pre_check_outcome", "") or "").strip()
        or str(source_snapshot.context.execution_context.get("pre_check_outcome") or "").strip()
    )
    if precheck_outcome:
        next_snapshot.context.execution_context["pre_check_outcome"] = precheck_outcome
    return next_snapshot.dump()


def _resolve_precheck_outcome_for_admin_attempt(*, source_run: Run | None, plan: object | None) -> str | None:
    if source_run is not None:
        persisted = str(getattr(source_run, "pre_check_outcome", "") or "").strip()
        if persisted:
            return persisted
    return resolve_precheck_outcome_from_plan(plan)


def _latest_decision_issue_labels_for_workflow(*, session, workflow) -> list[str]:  # noqa: ANN001
    event = session.execute(
        select(DecisionEvent)
        .where(
            DecisionEvent.tenant_id == workflow.tenant_id,
            DecisionEvent.issue_key == workflow.issue_key,
        )
        .order_by(desc(DecisionEvent.created_at))
        .limit(1)
    ).scalar_one_or_none()
    if event is None or not isinstance(event.payload_json, dict):
        return []
    labels = event.payload_json.get("issue_labels", [])
    if not isinstance(labels, list):
        return []
    return [str(label).strip() for label in labels if str(label).strip()]


def _resolve_required_worker_capability_for_admin_attempt(*, session, workflow, source_run: Run | None, plan: object | None) -> str | None:  # noqa: ANN001
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
            DecisionCase.issue_key == workflow.issue_key,
        )
        .limit(1)
    ).scalar_one_or_none()
    case_capability = str(getattr(case, "required_worker_capability", "") or "").strip()
    if case_capability:
        return case_capability
    inferred = infer_required_worker_capability(
        issue_summary=workflow.issue_summary,
        issue_description=workflow.issue_description,
        issue_labels=_latest_decision_issue_labels_for_workflow(session=session, workflow=workflow),
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        issue_key=workflow.issue_key,
    )
    return str(inferred or "").strip() or None


def _require_ready_for_queue(*, source: str, plan: object | None, precheck_outcome: str | None) -> None:
    try:
        require_ready_for_agent_enqueue(
            source=source,
            precheck_outcome=precheck_outcome,
            precheck_source_plan=plan,
        )
    except RunStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _cancel_open_input_requests(*, session, workflow_id: str) -> None:  # noqa: ANN001
    open_requests = session.execute(
        select(RunHumanInputRequest).where(
            RunHumanInputRequest.workflow_id == workflow_id,
            RunHumanInputRequest.status.in_(("pending", "answered")),
        )
    ).scalars().all()
    for request in open_requests:
        request.status = "cancelled"


def _next_attempt_number(*, session, workflow_id: str) -> int:  # noqa: ANN001
    existing_runs = _workflow_runs(session=session, workflow_id=workflow_id)
    return (max((run.attempt_number for run in existing_runs), default=0) or 0) + 1


def _is_active_scope_unique_violation(error: IntegrityError) -> bool:
    message = str(error).lower()
    if "uq_workflow_executions_active_scope" in message:
        return True
    return (
        "workflow_executions.tenant_id" in message
        and "workflow_executions.issue_key" in message
        and "workflow_executions.dedupe_scope" in message
    )


def list_workflows(
    *,
    session,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    issue_query: str | None,
    limit: int,
    offset: int,
    workflow_to_schema_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    query = select(WorkflowExecution)
    if tenant_id:
        query = query.where(WorkflowExecution.tenant_id == tenant_id)
    if project_id:
        query = query.where(WorkflowExecution.project_id == project_id)
    if status_filter:
        query = query.where(WorkflowExecution.status == status_filter)
    normalized_issue = str(issue_query or "").strip()
    if normalized_issue:
        like_value = f"%{normalized_issue}%"
        query = query.where(
            or_(
                WorkflowExecution.issue_key.ilike(like_value),
                WorkflowExecution.issue_summary.ilike(like_value),
            )
        )
    query = query.order_by(desc(WorkflowExecution.created_at)).limit(limit).offset(offset)
    return [
        _workflow_schema(
            session=session,
            workflow=workflow,
            workflow_to_schema_fn=workflow_to_schema_fn,
            run_to_schema_fn=run_to_schema_fn,
        )
        for workflow in session.execute(query).scalars().all()
    ]


def get_workflow(
    *,
    session,
    workflow_id: str,
    workflow_to_schema_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
    return _workflow_schema(
        session=session,
        workflow=workflow,
        workflow_to_schema_fn=workflow_to_schema_fn,
        run_to_schema_fn=run_to_schema_fn,
    )


def create_workflow_attempt(
    *,
    session,
    workflow_id: str,
    mode: str,
    checkpoint_kind: str | None,
    tenant_model,
    run_to_schema_fn,
):  # noqa: ANN001
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")

    _reconcile_workflow_status_with_active_attempt(session=session, workflow=workflow)
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ATTEMPT_ENTRY_MODES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid attempt mode")
    creation_policy = attempt_creation_policy(workflow_status=workflow.status, mode=normalized_mode)
    if not creation_policy.allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow already has an active attempt",
        )
    selected_checkpoint = None
    source_run = _latest_run_for_workflow(session=session, workflow_id=workflow_id)
    if normalized_mode != "fresh":
        selected_checkpoint = _latest_checkpoint_for_kind(
            session=session,
            workflow_id=workflow_id,
            checkpoint_kind=str(checkpoint_kind or "").strip(),
        )
        if selected_checkpoint is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No checkpoint is available for that kind")

    tenant = session.get(tenant_model, workflow.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found for workflow")

    if normalized_mode == "fresh":
        project = _resolve_project_for_fresh_start(session=session, workflow=workflow, source_run=source_run)
    else:
        project = _resolve_project(session=session, workflow=workflow, selected_checkpoint=selected_checkpoint)
    if project is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active project mapping found for workflow")
    if bool(getattr(project, "is_archived", False)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Project {project.project_id} is archived")

    same_workflow = creation_policy.reuse_workflow

    now = _now()
    next_workflow = workflow
    if not same_workflow:
        next_workflow = WorkflowExecution(
            workflow_id=str(uuid4()),
            tenant_id=workflow.tenant_id,
            project_id=project.project_id,
            issue_key=workflow.issue_key,
            issue_summary=workflow.issue_summary,
            issue_description=workflow.issue_description,
            repo_url=workflow.repo_url,
            branch=None if normalized_mode == "fresh" else workflow.branch,
            pr_url=None if normalized_mode == "fresh" else workflow.pr_url,
            dedupe_scope=workflow.dedupe_scope,
            status="queued",
            last_error=None,
            active_run_id=None,
            latest_checkpoint_id=selected_checkpoint.checkpoint_id if selected_checkpoint is not None else None,
            source_workflow_id=workflow.workflow_id,
            source_run_id=(source_run.run_id if normalized_mode == "fresh" and source_run is not None else selected_checkpoint.run_id),
            blocked_reason=None,
            created_at=now,
            started_at=None,
            finished_at=None,
            updated_at=now,
        )
        session.add(next_workflow)
    else:
        _cancel_open_input_requests(session=session, workflow_id=workflow.workflow_id)
        next_workflow.status = "queued"
        next_workflow.last_error = None
        next_workflow.blocked_reason = None
        next_workflow.finished_at = None
        next_workflow.latest_checkpoint_id = selected_checkpoint.checkpoint_id if selected_checkpoint is not None else None
        next_workflow.updated_at = now

    next_run_plan = _fresh_start_plan(source_run=source_run) if normalized_mode == "fresh" else dict(selected_checkpoint.payload_json or {})
    next_run_precheck_outcome = _resolve_precheck_outcome_for_admin_attempt(
        source_run=source_run,
        plan=next_run_plan,
    )
    next_run_required_worker_capability = _resolve_required_worker_capability_for_admin_attempt(
        session=session,
        workflow=next_workflow,
        source_run=source_run,
        plan=next_run_plan,
    )

    next_run = Run(
        run_id=str(uuid4()),
        workflow_id=next_workflow.workflow_id,
        tenant_id=next_workflow.tenant_id,
        project_id=project.project_id,
        issue_key=next_workflow.issue_key,
        issue_summary=next_workflow.issue_summary,
        issue_description=next_workflow.issue_description,
        repo_url=next_workflow.repo_url,
        branch=next_workflow.branch,
        pr_url=next_workflow.pr_url,
        attempt_number=1 if not same_workflow else _next_attempt_number(session=session, workflow_id=workflow.workflow_id),
        parent_run_id=(source_run.run_id if normalized_mode == "fresh" and source_run is not None else selected_checkpoint.run_id if selected_checkpoint is not None else None),
        entry_mode=normalized_mode,
        entry_stage="orchestrated" if normalized_mode == "fresh" else selected_checkpoint.stage,
        entry_checkpoint_id=None if normalized_mode == "fresh" else selected_checkpoint.checkpoint_id,
        dedupe_scope=next_workflow.dedupe_scope,
        status="queued",
        last_error=None,
        pre_check_outcome=next_run_precheck_outcome,
        required_worker_capability=next_run_required_worker_capability,
        plan=next_run_plan,
        created_at=now,
        dispatch_claimed_at=None,
        started_at=None,
        last_heartbeat_at=None,
        worker_service_instance_id=None,
        finished_at=None,
    )
    _require_ready_for_queue(
        source="admin_workflow_attempt",
        plan=next_run.plan,
        precheck_outcome=next_run.pre_check_outcome,
    )
    session.add(next_run)
    next_workflow.active_run_id = next_run.run_id
    notify_run_enqueued(
        session,
        tenant_id=next_workflow.tenant_id,
        project_id=next_workflow.project_id,
        run_id=next_run.run_id,
        issue_key=next_workflow.issue_key,
    )
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if _is_active_scope_unique_violation(error):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "A queued or in-progress workflow already exists for this issue "
                    "and dedupe scope. Resume the active workflow instead."
                ),
            ) from error
        raise
    session.refresh(next_run)
    return run_to_schema_fn(next_run)
