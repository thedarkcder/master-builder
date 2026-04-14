from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, desc, or_, select
from fastapi import HTTPException, status

from orchestrator.storage.models import AgentLifecycleEvent, RunLogEvent, WorkflowExecution


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
    payload = run_to_schema_fn(run)
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
    workflow = session.get(WorkflowExecution, run.workflow_id)
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found for run")

    now = datetime.now(timezone.utc)
    run.status = "cancelled"
    run.last_error = f"Cancelled by {cancelled_by}"
    run.started_at = run.started_at or now
    run.finished_at = now
    workflow.status = "cancelled"
    workflow.last_error = run.last_error
    workflow.finished_at = now
    workflow.updated_at = now
    session.commit()
    session.refresh(run)
    return run_to_schema_fn(run)


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


def list_run_log_events(
    *,
    session,
    run_id: str,
    run_model,
    run_log_schema_cls,
    limit: int = 200,
    before_recorded_at: datetime | None = None,
    before_event_id: str | None = None,
):  # noqa: ANN001
    run = session.get(run_model, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    query = select(RunLogEvent).where(RunLogEvent.run_id == run_id)
    normalized_before_event_id = str(before_event_id or "").strip()
    if before_recorded_at is not None:
        if normalized_before_event_id:
            query = query.where(
                or_(
                    RunLogEvent.recorded_at < before_recorded_at,
                    and_(
                        RunLogEvent.recorded_at == before_recorded_at,
                        RunLogEvent.event_id < normalized_before_event_id,
                    ),
                )
            )
        else:
            query = query.where(RunLogEvent.recorded_at < before_recorded_at)
    query = query.order_by(desc(RunLogEvent.recorded_at), desc(RunLogEvent.event_id)).limit(
        max(1, min(limit, 1000))
    )
    log_rows = session.execute(query).scalars().all()
    return [
        run_log_schema_cls(
            run_id=row.run_id,
            issue_key=row.issue_key,
            project_id=row.project_id,
            agent_id=row.agent_id,
            invocation_id=row.invocation_id,
            channel=row.channel,
            command=row.command,
            working_dir=row.working_dir,
            stage=row.stage,
            attempt=row.attempt,
            stream=row.stream,
            message=row.message,
            recorded_at=row.recorded_at,
        )
        for row in log_rows
    ]
