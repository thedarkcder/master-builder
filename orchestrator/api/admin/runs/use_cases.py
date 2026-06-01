from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.admin.runs.logging_stream_service import stream_run_events_ndjson as stream_run_events_ndjson_impl
from orchestrator.api.admin.runs.query import build_runs_query as build_runs_query_impl
from orchestrator.api.admin.runs.runtime_logs_service import (
    list_runtime_log_events as list_runtime_log_events_impl,
    stream_runtime_events_ndjson as stream_runtime_events_ndjson_impl,
)
from orchestrator.api.admin.runs.service import (
    cancel_run_admin as cancel_run_admin_impl,
    create_run_preview_admin as create_run_preview_admin_impl,
    get_run as get_run_impl,
    list_run_events as list_run_events_impl,
    list_run_logging_pane_events as list_run_logging_pane_events_impl,
    list_runs as list_runs_impl,
)
from orchestrator.api.admin.schema_mappers import run_to_schema
from orchestrator.api.schemas import LoggingPaneEventRead, ProjectDeploymentReleaseRead, RunEventRead, RunRead
from orchestrator.core.config import get_settings
from orchestrator.core.integrations.atlassian.links import tenant_jira_issue_url
from orchestrator.core.platform.access import PERMISSION_PROJECTS_MANAGE
from orchestrator.core.security import AuthenticatedPrincipal, require_tenant_permission, require_tenant_workspace_access
from orchestrator.storage.models import Project, Run, Tenant


def list_runs(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    issue_query: str | None,
    pr_state: str | None,
    from_time: datetime | None,
    to_time: datetime | None,
    limit: int,
    offset: int,
) -> list[RunRead]:
    if not principal.is_platform_super_admin:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for tenant-scoped run listing",
            )
        require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return list_runs_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        issue_query=issue_query,
        pr_state=pr_state,
        from_time=from_time,
        to_time=to_time,
        limit=limit,
        offset=offset,
        build_runs_query_fn=build_runs_query_impl,
        run_to_schema_fn=run_to_schema,
        tenant_model=Tenant,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
    )


def get_run(*, session: Session, principal: AuthenticatedPrincipal, run_id: str) -> RunRead:
    if not principal.is_platform_super_admin:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        require_tenant_workspace_access(principal=principal, tenant_id=run.tenant_id)
    return get_run_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_to_schema_fn=run_to_schema,
        tenant_model=Tenant,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
    )

def cancel_run(*, session: Session, run_id: str) -> RunRead:
    return cancel_run_admin_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_to_schema_fn=run_to_schema,
        cancelled_by="admin",
    )


def create_run_preview(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    run_id: str,
    force: bool = False,
) -> ProjectDeploymentReleaseRead:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    require_tenant_permission(
        principal=principal,
        tenant_id=run.tenant_id,
        permission_key=PERMISSION_PROJECTS_MANAGE,
    )
    return create_run_preview_admin_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        tenant_model=Tenant,
        project_model=Project,
        settings=get_settings(),
        force=force,
    )


def list_run_events(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    run_id: str,
    limit: int,
) -> list[RunEventRead]:
    _require_run_access(session=session, principal=principal, run_id=run_id)
    return list_run_events_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_event_schema_cls=RunEventRead,
        limit=limit,
    )


def list_run_logs(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    run_id: str,
    limit: int,
    before_recorded_at: datetime | None,
    before_event_id: str | None,
) -> list[LoggingPaneEventRead]:
    _require_run_access(session=session, principal=principal, run_id=run_id)
    return list_run_logging_pane_events_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        logging_pane_schema_cls=LoggingPaneEventRead,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )


def stream_run_events(
    *,
    session: Session,
    principal: AuthenticatedPrincipal,
    run_id: str,
) -> Iterator[str]:
    _require_run_access(session=session, principal=principal, run_id=run_id)
    return stream_run_events_ndjson_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        settings=get_settings(),
    )


def list_runtime_logs(
    *,
    session: Session,
    tenant_id: str | None,
    project_id: str | None,
    run_id: str | None,
    channel: str | None,
    command: str | None,
    limit: int,
) -> list[LoggingPaneEventRead]:
    return list_runtime_log_events_impl(
        session=session,
        logging_pane_schema_cls=LoggingPaneEventRead,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
        limit=limit,
    )


def stream_runtime_events(
    *,
    session: Session,
    tenant_id: str | None,
    project_id: str | None,
    run_id: str | None,
    channel: str | None,
    command: str | None,
) -> Iterator[str]:
    return stream_runtime_events_ndjson_impl(
        session=session,
        settings=get_settings(),
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
    )

def _require_run_access(*, session: Session, principal: AuthenticatedPrincipal, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if not principal.is_platform_super_admin:
        require_tenant_workspace_access(principal=principal, tenant_id=run.tenant_id)
    return run
