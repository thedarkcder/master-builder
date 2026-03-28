from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.jira_connect_flow import (
    build_jira_connect_start as build_jira_connect_start_impl,
    handle_jira_connect_callback as handle_jira_connect_callback_impl,
)
from orchestrator.api.admin.jira_route_service import (
    get_jira_webhook_diagnostics as get_jira_webhook_diagnostics_route_impl,
    list_jira_projects_for_connection as list_jira_projects_for_connection_impl,
    run_tenant_jira_webhook_action as run_tenant_jira_webhook_action_impl,
)
from orchestrator.api.admin.jira_webhook_helpers import (
    jira_webhook_callback_url,
    parse_managed_webhook_ids,
)
from orchestrator.api.admin.jira_webhook_response_helpers import (
    build_jira_webhook_diagnostics as build_jira_webhook_diagnostics_impl,
    jira_webhook_action_status_code,
)
from orchestrator.api.admin.tenant_actions import (
    disconnect_tenant_jira as disconnect_tenant_jira_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    JiraConnectStart,
    JiraProjectRead,
    JiraWebhookActionResult,
    JiraWebhookDiagnosticsRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_any_tenant_permission,
    require_authenticated_principal,
    require_tenant_permission,
)
from orchestrator.core.tenant_access import PERMISSION_PROJECTS_MANAGE, PERMISSION_WORKSPACE_MANAGE
from orchestrator.storage.models import JiraOAuthConnection, Tenant

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post("/jira/connect/start", response_model=JiraConnectStart)
def start_jira_connect(
    return_to: str = Query(default="wizard", pattern="^(wizard|edit)$"),
    tenant_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> JiraConnectStart:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    elif tenant_id:
        require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_WORKSPACE_MANAGE)
    return build_jira_connect_start_impl(
        return_to=return_to,
        tenant_id=tenant_id,
        session=session,
        settings=get_settings(),
        jira_oauth_client_fn=deps.jira_oauth_client,
    )


@router.get("/jira/connect/callback", include_in_schema=False)
def jira_connect_callback(
    code: str = Query(..., min_length=1),
    state_token: str = Query(..., alias="state"),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect_url = handle_jira_connect_callback_impl(
        code=code,
        state_token=state_token,
        session=session,
        settings=get_settings(),
        jira_oauth_client_fn=deps.jira_oauth_client,
        auto_provision_jira_webhook_fn=lambda *, session, tenant, settings: deps.provision_jira_webhook(
            session=session,
            tenant=tenant,
            settings=settings,
            replace_existing=True,
        ),
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/jira/connections/{connection_id}/projects", response_model=list[JiraProjectRead])
def list_jira_projects_for_connection(
    connection_id: str,
    tenant_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[JiraProjectRead]:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    elif tenant_id:
        require_any_tenant_permission(
            principal=principal,
            tenant_id=tenant_id,
            permission_keys=(PERMISSION_WORKSPACE_MANAGE, PERMISSION_PROJECTS_MANAGE),
        )
    return list_jira_projects_for_connection_impl(
        session=session,
        connection_id=connection_id,
        jira_oauth_connection_model=JiraOAuthConnection,
        settings=get_settings(),
        refresh_jira_connection_tokens_fn=deps.refresh_jira_connection_tokens,
        jira_oauth_client_fn=deps.jira_oauth_client,
    )


@router.get("/tenants/{tenant_id}/jira/webhooks/diagnostics", response_model=JiraWebhookDiagnosticsRead)
def get_jira_webhook_diagnostics(
    tenant_id: str,
    within_minutes: int = Query(default=60, ge=1, le=1440),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookDiagnosticsRead:
    return get_jira_webhook_diagnostics_route_impl(
        session=session,
        tenant_id=tenant_id,
        within_minutes=within_minutes,
        tenant_model=Tenant,
        settings=get_settings(),
        build_jira_webhook_diagnostics_fn=build_jira_webhook_diagnostics_impl,
        jira_webhook_callback_url_fn=jira_webhook_callback_url,
        parse_managed_webhook_ids_fn=parse_managed_webhook_ids,
    )


@router.post("/tenants/{tenant_id}/jira/webhooks/provision", response_model=JiraWebhookActionResult)
def provision_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return run_tenant_jira_webhook_action_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        provision_jira_webhook_fn=deps.provision_jira_webhook,
        jira_webhook_action_status_code_fn=jira_webhook_action_status_code,
        replace_existing=False,
    )


@router.post("/tenants/{tenant_id}/jira/webhooks/reset", response_model=JiraWebhookActionResult)
def reset_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return run_tenant_jira_webhook_action_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        provision_jira_webhook_fn=deps.provision_jira_webhook,
        jira_webhook_action_status_code_fn=jira_webhook_action_status_code,
        replace_existing=True,
    )


@router.post("/tenants/{tenant_id}/jira/disconnect", response_model=JiraWebhookActionResult)
def disconnect_tenant_jira(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return disconnect_tenant_jira_impl(
        session=session,
        tenant=session.get(Tenant, tenant_id),
        tenant_id=tenant_id,
        settings=get_settings(),
        delete_jira_webhooks_fn=deps.delete_jira_webhooks,
    )
