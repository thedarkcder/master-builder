from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, desc, or_, select
from fastapi import HTTPException, status

from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import RunStateTransitionError, cancel_run
from orchestrator.core.runs import enqueue_run
from orchestrator.core.communications.enqueue_reason_contract import format_enqueue_conflict_detail
from orchestrator.storage.models import AgentLifecycleEvent, RunLogEvent


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


def rerun_run(
    *,
    session,
    run_id: str,
    run_model,
    tenant_model,
    resolve_project_for_run_fn,
    run_to_schema_fn,
):  # noqa: ANN001
    source_run = session.get(run_model, run_id)
    if source_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    tenant = session.get(tenant_model, source_run.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found for run")
    project = resolve_project_for_run_fn(session, run=source_run)
    if project is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active project mapping found for run")
    archived = bool(getattr(project, "is_archived", False))
    if archived:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Project {project.project_id} is archived")
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config,
        project_overrides=project.policy_overrides,
    )
    enqueue_result = enqueue_run(
        session,
        tenant_id=source_run.tenant_id,
        project_id=project.project_id,
        issue_key=source_run.issue_key,
        issue_summary=source_run.issue_summary,
        issue_description=source_run.issue_description,
        repo_url=project.github_repository,
        delivery_id=None,
        max_concurrent_runs=effective_policy.get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=format_enqueue_conflict_detail(
                prefix="Run could not be rerun",
                enqueue_reason=str(enqueue_result.reason),
                enqueue_run_obj=enqueue_result.run,
            ),
        )
    return run_to_schema_fn(enqueue_result.run)


def cancel_run_admin(
    *,
    session,
    run_id: str,
    run_to_schema_fn,
    cancelled_by: str = "admin",
):  # noqa: ANN001
    try:
        cancelled = cancel_run(session, run_id=run_id, cancelled_by=cancelled_by)
    except RunStateTransitionError as exc:
        message = str(exc)
        status_code = status.HTTP_404_NOT_FOUND if "Run not found" in message else status.HTTP_409_CONFLICT
        raise HTTPException(status_code=status_code, detail=message) from exc
    return run_to_schema_fn(cancelled)


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
