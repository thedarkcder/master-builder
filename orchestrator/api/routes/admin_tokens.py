from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    TokenCompareRead,
    TokenCompareRequest,
    TokenOverviewRead,
    TokenStageDiagnosticsCompareRead,
    TokenStageDiagnosticsRead,
    TokenTimelineRead,
)
from orchestrator.api.admin.token_compare_service import compare_run_tokens
from orchestrator.api.admin.token_diagnostics_compare_service import (
    get_token_stage_diagnostics_compare,
)
from orchestrator.api.admin.token_diagnostics_service import get_token_stage_diagnostics
from orchestrator.api.admin.token_overview_service import get_token_overview
from orchestrator.api.admin.token_timeline_service import get_run_token_timeline
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get(
    "/tenants/{tenant_id}/runs/{run_id}/token-timeline",
    response_model=TokenTimelineRead,
)
def get_tenant_run_token_timeline_route(
    tenant_id: str,
    run_id: str,
    stage: str | None = Query(default=None),
    attempt: int | None = Query(default=None),
    include_retries: bool = Query(default=False),
    model: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TokenTimelineRead:
    return get_run_token_timeline(
        session=session,
        tenant_id=tenant_id,
        run_id=run_id,
        stage=stage,
        attempt=attempt,
        include_retries=include_retries,
        model=model,
    )


@router.get("/tenants/{tenant_id}/token-overview", response_model=TokenOverviewRead)
def get_tenant_token_overview_route(
    tenant_id: str,
    project_id: str = Query(...),
    issue_key: str | None = Query(default=None),
    run_status: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    attempt: int | None = Query(default=None),
    model: str | None = Query(default=None),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    only_retried: bool = Query(default=False),
    only_with_test_stage: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=200),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TokenOverviewRead:
    return get_token_overview(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        run_status=run_status,
        stage=stage,
        attempt=attempt,
        model=model,
        start_date=start_date,
        end_date=end_date,
        only_retried=only_retried,
        only_with_test_stage=only_with_test_stage,
        page=page,
        page_size=page_size,
    )


@router.post("/tenants/{tenant_id}/token-compare", response_model=TokenCompareRead)
def compare_tokens_tenant_route(
    tenant_id: str,
    payload: TokenCompareRequest,
    project_id: str = Query(...),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TokenCompareRead:
    return compare_run_tokens(
        session=session,
        payload=payload,
        tenant_id=tenant_id,
        project_id=project_id,
    )


@router.get(
    "/tenants/{tenant_id}/token-stage-diagnostics",
    response_model=TokenStageDiagnosticsRead,
)
def get_tenant_token_stage_diagnostics_route(
    tenant_id: str,
    project_id: str = Query(...),
    issue_key: str | None = Query(default=None),
    run_status: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    attempt: int | None = Query(default=None),
    model: str | None = Query(default=None),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    only_retried: bool = Query(default=False),
    only_with_test_stage: bool = Query(default=False),
    page: int = Query(default=1, ge=1, le=10000),
    page_size: int = Query(default=20, ge=1, le=200),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TokenStageDiagnosticsRead:
    return get_token_stage_diagnostics(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        run_status=run_status,
        stage=stage,
        attempt=attempt,
        model=model,
        start_date=start_date,
        end_date=end_date,
        only_retried=only_retried,
        only_with_test_stage=only_with_test_stage,
        page=page,
        page_size=page_size,
    )


@router.get(
    "/tenants/{tenant_id}/token-stage-diagnostics-compare",
    response_model=TokenStageDiagnosticsCompareRead,
)
def get_tenant_token_stage_diagnostics_compare_route(
    tenant_id: str,
    project_ids: str = Query(...),
    issue_key: str | None = Query(default=None),
    run_status: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    attempt: int | None = Query(default=None),
    model: str | None = Query(default=None),
    start_date: datetime | None = Query(default=None),
    end_date: datetime | None = Query(default=None),
    only_retried: bool = Query(default=False),
    only_with_test_stage: bool = Query(default=False),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TokenStageDiagnosticsCompareRead:
    return get_token_stage_diagnostics_compare(
        session=session,
        tenant_id=tenant_id,
        project_ids=project_ids,
        issue_key=issue_key,
        run_status=run_status,
        stage=stage,
        attempt=attempt,
        model=model,
        start_date=start_date,
        end_date=end_date,
        only_retried=only_retried,
        only_with_test_stage=only_with_test_stage,
    )
