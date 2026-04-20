from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.admin.codex_logs_service import (
    list_codex_log_events as list_codex_log_events_impl,
    stream_codex_events_ndjson as stream_codex_events_ndjson_impl,
)
from orchestrator.api.admin.run_event_stream_service import (
    stream_run_events_ndjson as stream_run_events_ndjson_impl,
)
from orchestrator.api.admin.runs_query import build_runs_query as build_runs_query_impl
from orchestrator.api.admin.runs_service import (
    cancel_run_admin as cancel_run_admin_impl,
    get_run as get_run_impl,
    list_run_events as list_run_events_impl,
    list_run_log_events as list_run_log_events_impl,
    list_runs as list_runs_impl,
)
from orchestrator.api.admin.schema_mappers import run_to_schema, workflow_to_schema
from orchestrator.api.admin.workflows_service import (
    create_workflow_attempt as create_workflow_attempt_impl,
    get_workflow as get_workflow_impl,
    get_workflow_type_detail as get_workflow_type_detail_impl,
    list_workflows as list_workflows_impl,
    list_workflow_types as list_workflow_types_impl,
    resume_workflow_execution as resume_workflow_execution_impl,
    retry_workflow_operation as retry_workflow_operation_impl,
    update_workflow_type_detail as update_workflow_type_detail_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    RunEventRead,
    RunLogEventRead,
    RunRead,
    WorkflowAttemptCreateRequest,
    WorkflowRead,
    WorkflowTypeDetailRead,
    WorkflowTypeSummaryRead,
    WorkflowTypeUpdateRequest,
)
from orchestrator.api.discord.ingress.seed_runtime import seed_issues_with_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.workflow_integration_router import WorkflowIntegrationRouter
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_authenticated_principal,
    require_tenant_workspace_access,
)
from orchestrator.storage.models import Run, Tenant, WorkflowExecution

router = APIRouter(prefix="/api/admin", tags=["admin"])
workflow_integration_router = WorkflowIntegrationRouter()

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None


@router.get("/runs", response_model=list[RunRead])
def list_runs(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    issue_query: str | None = Query(default=None, alias="issue"),
    pr_state: str | None = Query(default=None, alias="pr_state"),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
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


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> RunRead:
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


@router.get("/workflows", response_model=list[WorkflowRead])
def list_workflows(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    issue_query: str | None = Query(default=None, alias="issue"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowRead]:
    if not principal.is_platform_super_admin:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for tenant-scoped workflow listing",
            )
        require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return list_workflows_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        status_filter=status_filter,
        issue_query=issue_query,
        limit=limit,
        offset=offset,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
    )


@router.get("/workflow-types", response_model=list[WorkflowTypeSummaryRead])
def list_workflow_types(
    tenant_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[WorkflowTypeSummaryRead]:
    if not principal.is_platform_super_admin:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for tenant-scoped workflow type listing",
            )
        require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return list_workflow_types_impl(
        session=session,
        tenant_id=tenant_id,
    )


@router.get("/workflow-types/{workflow_type_key}", response_model=WorkflowTypeDetailRead)
def get_workflow_type_detail(
    workflow_type_key: str,
    tenant_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowTypeDetailRead:
    if not principal.is_platform_super_admin:
        if not tenant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="tenant_id is required for tenant-scoped workflow type detail",
            )
        require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    return get_workflow_type_detail_impl(
        session=session,
        workflow_type_key=workflow_type_key,
        tenant_id=tenant_id,
    )


@router.put("/workflow-types/{workflow_type_key}", response_model=WorkflowTypeDetailRead)
def update_workflow_type_detail(
    workflow_type_key: str,
    payload: WorkflowTypeUpdateRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WorkflowTypeDetailRead:
    return update_workflow_type_detail_impl(
        session=session,
        workflow_type_key=workflow_type_key,
        tenant_id=None,
        payload=payload,
    )


@router.get("/workflows/{execution_id}", response_model=WorkflowRead)
def get_workflow(
    execution_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WorkflowRead:
    if not principal.is_platform_super_admin:
        workflow = session.execute(
            select(WorkflowExecution)
            .where(WorkflowExecution.execution_id == execution_id)
            .limit(1)
        ).scalar_one_or_none()
        if workflow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow not found")
        require_tenant_workspace_access(principal=principal, tenant_id=workflow.tenant_id)
    return get_workflow_impl(
        session=session,
        execution_id=execution_id,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
        integration_router=workflow_integration_router,
    )


@router.post("/workflows/{execution_id}/attempts", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def create_workflow_attempt(
    execution_id: str,
    payload: WorkflowAttemptCreateRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return create_workflow_attempt_impl(
        session=session,
        execution_id=execution_id,
        mode=payload.mode,
        checkpoint_kind=payload.checkpoint_kind,
        tenant_model=Tenant,
        run_to_schema_fn=run_to_schema,
    )


@router.post("/workflows/{execution_id}/resume", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def resume_workflow_execution(
    execution_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return resume_workflow_execution_impl(
        session=session,
        execution_id=execution_id,
        tenant_model=Tenant,
        run_to_schema_fn=run_to_schema,
    )


@router.post("/workflows/{execution_id}/operations/{operation_id}/retry", response_model=WorkflowRead)
def retry_workflow_operation(
    execution_id: str,
    operation_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WorkflowRead:
    return retry_workflow_operation_impl(
        session=session,
        execution_id=execution_id,
        operation_id=operation_id,
        workflow_to_schema_fn=workflow_to_schema,
        run_to_schema_fn=run_to_schema,
        integration_router=workflow_integration_router,
        build_runtime_for_selector_fn=build_runtime_for_selector,
        seed_issues_with_runtime_fn=seed_issues_with_runtime,
    )


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
def cancel_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return cancel_run_admin_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_to_schema_fn=run_to_schema,
        cancelled_by="admin",
    )


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead])
def list_run_events(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[RunEventRead]:
    if not principal.is_platform_super_admin:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        require_tenant_workspace_access(principal=principal, tenant_id=run.tenant_id)
    return list_run_events_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_event_schema_cls=RunEventRead,
        limit=limit,
    )


@router.get("/runs/{run_id}/logs", response_model=list[RunLogEventRead])
def list_run_logs(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    before_recorded_at: datetime | None = Query(default=None),
    before_event_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[RunLogEventRead]:
    if not principal.is_platform_super_admin:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
        require_tenant_workspace_access(principal=principal, tenant_id=run.tenant_id)
    return list_run_log_events_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_log_schema_cls=RunLogEventRead,
        limit=limit,
        before_recorded_at=before_recorded_at,
        before_event_id=before_event_id,
    )


@router.get("/runs/{run_id}/events/stream")
def stream_run_events(
    run_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if not principal.is_platform_super_admin:
        require_tenant_workspace_access(principal=principal, tenant_id=run.tenant_id)
    return StreamingResponse(
        stream_run_events_ndjson_impl(
            session=session,
            run_id=run_id,
            run_model=Run,
            settings=get_settings(),
            psycopg_module=psycopg,
        ),
        media_type="application/x-ndjson",
    )


@router.get("/codex/logs", response_model=list[RunLogEventRead])
def list_codex_logs(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    command: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunLogEventRead]:
    return list_codex_log_events_impl(
        session=session,
        run_log_schema_cls=RunLogEventRead,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
        limit=limit,
    )


@router.get("/codex/events/stream")
def stream_codex_events(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    command: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    return StreamingResponse(
        stream_codex_events_ndjson_impl(
            session=session,
            settings=get_settings(),
            psycopg_module=psycopg,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            channel=channel,
            command=command,
        ),
        media_type="application/x-ndjson",
    )
