from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import desc, or_, select

from orchestrator.core.workflow.transitions import ACTIVE_WORKFLOW_STATUSES
from orchestrator.storage.models import Project, Run, RunHumanInputRequest, WorkflowCheckpoint, WorkflowExecution
from orchestrator.storage.run_queue_events import notify_run_enqueued


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    next_plan: dict[str, object] = {}
    trigger_context = source_run.plan.get("trigger_context")
    if isinstance(trigger_context, dict) and trigger_context:
        next_plan["trigger_context"] = dict(trigger_context)
    pre_check = source_run.plan.get("pre_check")
    if isinstance(pre_check, dict) and pre_check:
        next_plan["pre_check"] = dict(pre_check)
    return next_plan or None


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

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"fresh", "restart", "resume"}:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid attempt mode")
    selected_checkpoint = None
    source_run = _latest_run_for_workflow(session=session, workflow_id=workflow_id)
    if normalized_mode == "fresh":
        if workflow.status in ACTIVE_WORKFLOW_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Workflow already has an active attempt",
            )
    else:
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

    same_workflow = normalized_mode != "fresh" and workflow.status == "waiting_for_input"
    if workflow.status in ACTIVE_WORKFLOW_STATUSES and not same_workflow:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Workflow already has an active attempt",
        )

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
        plan=_fresh_start_plan(source_run=source_run) if normalized_mode == "fresh" else dict(selected_checkpoint.payload_json or {}),
        created_at=now,
        started_at=None,
        last_heartbeat_at=None,
        worker_service_instance_id=None,
        finished_at=None,
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
    session.commit()
    session.refresh(next_run)
    return run_to_schema_fn(next_run)
