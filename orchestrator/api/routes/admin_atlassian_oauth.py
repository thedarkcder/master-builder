from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.atlassian_connect_flow import (
    build_atlassian_connect_start as build_atlassian_connect_start_impl,
    handle_atlassian_connect_callback as handle_atlassian_connect_callback_impl,
)
from orchestrator.api.admin.tenant_actions import (
    disconnect_tenant_atlassian as disconnect_tenant_atlassian_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import AtlassianConnectStart, JiraWebhookActionResult
from orchestrator.core.config import get_settings
from orchestrator.core.security import AuthenticatedPrincipal, require_admin, require_authenticated_principal, require_tenant_permission
from orchestrator.core.platform.access import PERMISSION_WORKSPACE_MANAGE
from orchestrator.storage.models import Tenant

router = APIRouter(prefix="/api/admin", tags=["admin", "atlassian-oauth"])


@router.post("/atlassian/connect/start", response_model=AtlassianConnectStart)
def start_atlassian_connect(
    return_to: str = Query(default="wizard", pattern="^(wizard|edit)$"),
    tenant_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> AtlassianConnectStart:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    elif tenant_id:
        require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_WORKSPACE_MANAGE)
    return build_atlassian_connect_start_impl(
        return_to=return_to,
        tenant_id=tenant_id,
        session=session,
        settings=get_settings(),
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
    )


@router.get("/atlassian/connect/callback", include_in_schema=False)
def atlassian_connect_callback(
    code: str = Query(..., min_length=1),
    state_token: str = Query(..., alias="state"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect_url = handle_atlassian_connect_callback_impl(
        code=code,
        state_token=state_token,
        session=session,
        settings=get_settings(),
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
        provision_jira_webhook_fn=deps.provision_jira_webhook,
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.post("/tenants/{tenant_id}/atlassian/disconnect", response_model=JiraWebhookActionResult)
def disconnect_tenant_atlassian(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return disconnect_tenant_atlassian_impl(
        session=session,
        tenant=session.get(Tenant, tenant_id),
        tenant_id=tenant_id,
        settings=get_settings(),
        delete_jira_webhooks_fn=deps.delete_jira_webhooks,
    )
