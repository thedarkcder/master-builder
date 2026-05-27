from __future__ import annotations

from datetime import datetime

from sqlalchemy import desc, select
from fastapi import HTTPException, status

from orchestrator.core.observability.logging_pane import list_run_logging_pane_events as list_run_logging_pane_events_core
from orchestrator.core.runs.service import RunStateTransitionError, cancel_run as cancel_run_execution
from orchestrator.storage.models import AgentLifecycleEvent, WorkflowExecution


def _with_issue_url(payload, issue_url: str | None):  # noqa: ANN001
    if hasattr(payload, "model_copy"):
        return payload.model_copy(update={"issue_url": issue_url})
    if isinstance(payload, dict):
        updated = dict(payload)
        updated["issue_url"] = issue_url
        return updated
    if hasattr(payload, "issue_url"):
        payload.issue_url = issue_url
    return payload


def list_runs(
    *,
    session,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    issue_query: str | None,
    pr_state: str | None,
    from_time,
    to_time,
    limit: int,
    offset: int,
    build_runs_query_fn,
    run_to_schema_fn,
    tenant_model,
    tenant_jira_issue_url_fn,
):  # noqa: ANN001
    query = build_runs_query_fn(
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        issue_query=issue_query,
        pr_state=pr_state,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
    )
    runs = session.execute(query).scalars().all()
    tenant_cache: dict[str, object | None] = {}
    payloads = []
    for run in runs:
        schema = run_to_schema_fn(run)
        run_tenant_id = str(getattr(run, "tenant_id", "") or "").strip()
        run_issue_key = str(getattr(run, "issue_key", "") or "").strip()
        current_tenant = tenant_cache.get(run_tenant_id) if run_tenant_id else None
        if run_tenant_id and run_tenant_id not in tenant_cache:
            current_tenant = session.get(tenant_model, run_tenant_id)
            tenant_cache[run_tenant_id] = current_tenant
        issue_url = (
            tenant_jira_issue_url_fn(
                session=session,
                tenant=current_tenant,
                issue_key=run_issue_key,
            )
            if current_tenant is not None and run_issue_key
            else None
        )
        payloads.append(_with_issue_url(schema, issue_url))
    return payloads


def get_run(*, session, run_id: str, run_model, run_to_schema_fn, tenant_model, tenant_jira_issue_url_fn):  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    workflow_execution_id = _workflow_execution_id_for_run(session=session, run=run)
    payload = run_to_schema_fn(run, workflow_execution_id=workflow_execution_id)
    tenant = session.get(tenant_model, run.tenant_id)
    issue_url = (
        tenant_jira_issue_url_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
        )
        if tenant is not None
        else None
    )
    return _with_issue_url(payload, issue_url)


def _workflow_execution_id_for_run(*, session, run) -> str | None:  # noqa: ANN001
    execution_id = session.execute(
        select(WorkflowExecution.execution_id)
        .where(WorkflowExecution.workflow_id == run.workflow_id)
        .limit(1)
    ).scalar_one_or_none()
    return str(execution_id or "").strip() or None


def cancel_run_admin(
    *,
    session,
    run_id: str,
    run_model,
    run_to_schema_fn,
    cancelled_by: str = "admin",
):  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.status in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot cancel run {run_id} from terminal status {run.status}",
        )
    try:
        cancelled_run = cancel_run_execution(
            session,
            run_id=run_id,
            cancelled_by=cancelled_by,
        )
        workflow_execution_id = _workflow_execution_id_for_run(session=session, run=cancelled_run)
        return run_to_schema_fn(cancelled_run, workflow_execution_id=workflow_execution_id)
    except RunStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def list_run_events(*, session, run_id: str, run_model, run_event_schema_cls, limit: int = 200):  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    event_rows = session.execute(
        select(AgentLifecycleEvent)
        .where(AgentLifecycleEvent.run_id == run_id)
        .order_by(desc(AgentLifecycleEvent.recorded_at), desc(AgentLifecycleEvent.event_id))
        .limit(max(1, min(limit, 500)))
    ).scalars().all()
    return [
        run_event_schema_cls(
            event_type=row.event_type,
            run_id=row.run_id,
            issue_key=row.issue_key,
            project_id=row.project_id,
            agent_id=row.agent_id,
            recorded_at=row.recorded_at,
        )
        for row in event_rows
    ]


def list_run_logging_pane_events(
    *,
    session,
    run_id: str,
    run_model,
    logging_pane_schema_cls,
    limit: int = 200,
    before_recorded_at: datetime | None = None,
    before_event_id: str | None = None,
):  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return list_run_logging_pane_events_core(
        session=session,
        run_id=run_id,
        schema_cls=logging_pane_schema_cls,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )
