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
from orchestrator.api.admin.jira_route_service import (
    list_jira_projects_for_connection as list_jira_projects_for_connection_impl,
)
from orchestrator.api.admin.confluence_route_service import (
    list_confluence_pages_for_tenant as list_confluence_pages_for_tenant_impl,
    list_confluence_spaces_for_tenant as list_confluence_spaces_for_tenant_impl,
)
from orchestrator.api.atlassian_oauth.connection_service import resolve_tenant_atlassian_connection
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    AtlassianConnectStart,
    ConfluencePageRead,
    ConfluenceSpaceCatalogRead,
    JiraProjectRead,
    JiraWebhookActionResult,
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
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant

router = APIRouter(prefix="/api/admin", tags=["admin"])


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
        auto_provision_jira_webhook_fn=lambda *, session, tenant, settings: deps.provision_jira_webhook(
            session=session,
            tenant=tenant,
            settings=settings,
            replace_existing=True,
        ),
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get("/atlassian/connections/{connection_id}/jira-projects", response_model=list[JiraProjectRead])
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
        atlassian_oauth_connection_model=AtlassianOAuthConnection,
        settings=get_settings(),
        refresh_atlassian_connection_tokens_fn=deps.refresh_atlassian_connection_tokens,
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
    )


@router.get("/tenants/{tenant_id}/atlassian/confluence/spaces", response_model=ConfluenceSpaceCatalogRead)
def list_confluence_spaces_for_tenant(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ConfluenceSpaceCatalogRead:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    else:
        require_any_tenant_permission(
            principal=principal,
            tenant_id=tenant_id,
            permission_keys=(PERMISSION_WORKSPACE_MANAGE, PERMISSION_PROJECTS_MANAGE),
        )
    return list_confluence_spaces_for_tenant_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        resolve_tenant_atlassian_connection_fn=resolve_tenant_atlassian_connection,
        refresh_atlassian_connection_tokens_fn=deps.refresh_atlassian_connection_tokens,
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
    )


@router.get("/tenants/{tenant_id}/atlassian/confluence/spaces/{space_key}/pages", response_model=list[ConfluencePageRead])
def list_confluence_pages_for_tenant(
    tenant_id: str,
    space_key: str,
    selected_page_id: str | None = Query(default=None),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[ConfluencePageRead]:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    else:
        require_any_tenant_permission(
            principal=principal,
            tenant_id=tenant_id,
            permission_keys=(PERMISSION_WORKSPACE_MANAGE, PERMISSION_PROJECTS_MANAGE),
        )
    return list_confluence_pages_for_tenant_impl(
        session=session,
        tenant_id=tenant_id,
        space_key=space_key,
        selected_page_id=selected_page_id,
        tenant_model=Tenant,
        settings=get_settings(),
        resolve_tenant_atlassian_connection_fn=resolve_tenant_atlassian_connection,
        refresh_atlassian_connection_tokens_fn=deps.refresh_atlassian_connection_tokens,
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
    )


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
