from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.integration_checks import (
    test_atlassian_connection as test_atlassian_connection_impl,
)
from orchestrator.api.admin.jira_webhook_helpers import default_ready_jql
from orchestrator.api.admin.ready_preview import (
    preview_tenant_ready_gate as preview_tenant_ready_gate_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import IntegrationTestResult, ReadyGatePreviewRead
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/tenants/{tenant_id}/ready-preview", response_model=ReadyGatePreviewRead)
def preview_tenant_ready_gate(
    tenant_id: str,
    max_results: int = Query(default=10, ge=1, le=50),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReadyGatePreviewRead:
    return preview_tenant_ready_gate_impl(
        session=session,
        tenant_id=tenant_id,
        max_results=max_results,
        settings=get_settings(),
        default_ready_jql_fn=default_ready_jql,
        refresh_atlassian_connection_tokens_fn=deps.refresh_atlassian_connection_tokens,
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
    )


@router.post("/tenants/{tenant_id}/test-atlassian", response_model=IntegrationTestResult)
def test_atlassian_connection(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> IntegrationTestResult:
    return test_atlassian_connection_impl(
        session=session,
        tenant_id=tenant_id,
        settings=get_settings(),
        refresh_atlassian_connection_tokens_fn=deps.refresh_atlassian_connection_tokens,
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
    )
