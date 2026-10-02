from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_authenticated_principal,
)
from orchestrator.api.admin.runs import use_cases
from orchestrator.api.schemas import (
    LoggingPaneEventRead,
    ProjectDeploymentReleaseRead,
    RunEventRead,
    RunRead,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
    return use_cases.list_runs(
        session=session,
        principal=principal,
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


@router.get("/runs/{run_id}", response_model=RunRead)
def get_run(
    run_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> RunRead:
    return use_cases.get_run(session=session, principal=principal, run_id=run_id)


@router.post("/runs/{run_id}/cancel", response_model=RunRead)
def cancel_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return use_cases.cancel_run(session=session, run_id=run_id)


@router.post("/runs/{run_id}/preview", response_model=ProjectDeploymentReleaseRead)
def create_run_preview(
    run_id: str,
    force: bool = Query(default=False),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectDeploymentReleaseRead:
    return use_cases.create_run_preview(
        session=session, principal=principal, run_id=run_id, force=force
    )


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead])
def list_run_events(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[RunEventRead]:
    return use_cases.list_run_events(
        session=session,
        principal=principal,
        run_id=run_id,
        limit=limit,
    )


@router.get("/runs/{run_id}/logs", response_model=list[LoggingPaneEventRead])
def list_run_logs(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    before_recorded_at: datetime | None = Query(default=None),
    before_event_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[LoggingPaneEventRead]:
    return use_cases.list_run_logs(
        session=session,
        principal=principal,
        run_id=run_id,
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
    return StreamingResponse(
        use_cases.stream_run_events(
            session=session, principal=principal, run_id=run_id
        ),
        media_type="application/x-ndjson",
    )


@router.get("/runtime/logs", response_model=list[LoggingPaneEventRead])
def list_runtime_logs(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    command: str | None = Query(default=None),
    limit: int = Query(default=500, ge=1, le=2000),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[LoggingPaneEventRead]:
    return use_cases.list_runtime_logs(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        channel=channel,
        command=command,
        limit=limit,
    )


@router.get("/runtime/events/stream")
def stream_runtime_events(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    run_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    command: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    return StreamingResponse(
        use_cases.stream_runtime_events(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            channel=channel,
            command=command,
        ),
        media_type="application/x-ndjson",
    )
