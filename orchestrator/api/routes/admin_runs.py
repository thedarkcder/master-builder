from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
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
    rerun_run as rerun_run_impl,
)
from orchestrator.api.admin.schema_mappers import run_to_schema
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import RunEventRead, RunLogEventRead, RunRead
from orchestrator.core.config import get_settings
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.security import require_admin
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run
from orchestrator.storage.models import Run, Tenant

router = APIRouter(prefix="/api/admin", tags=["admin"])

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
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunRead]:
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
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return get_run_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        run_to_schema_fn=run_to_schema,
        tenant_model=Tenant,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
    )


@router.post("/runs/{run_id}/rerun", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def rerun_failed_run(
    run_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    return rerun_run_impl(
        session=session,
        run_id=run_id,
        run_model=Run,
        tenant_model=Tenant,
        resolve_project_for_run_fn=resolve_project_for_run,
        run_to_schema_fn=run_to_schema,
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
        run_to_schema_fn=run_to_schema,
        cancelled_by="admin",
    )


@router.get("/runs/{run_id}/events", response_model=list[RunEventRead])
def list_run_events(
    run_id: str,
    limit: int = Query(default=200, ge=1, le=500),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunEventRead]:
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
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RunLogEventRead]:
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
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
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
