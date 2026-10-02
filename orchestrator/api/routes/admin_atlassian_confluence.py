from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.confluence_route_service import (
    list_confluence_pages_for_tenant as list_confluence_pages_for_tenant_impl,
    list_confluence_spaces_for_tenant as list_confluence_spaces_for_tenant_impl,
)
from orchestrator.api.atlassian_oauth.connection_service import (
    resolve_tenant_atlassian_connection,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import ConfluencePageRead, ConfluenceSpaceCatalogRead
from orchestrator.core.config import get_settings
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_any_tenant_permission,
    require_authenticated_principal,
)
from orchestrator.core.platform.access import (
    PERMISSION_PROJECTS_MANAGE,
    PERMISSION_WORKSPACE_MANAGE,
)
from orchestrator.storage.models import Tenant

router = APIRouter(prefix="/api/admin", tags=["admin", "atlassian-confluence"])


@router.get(
    "/tenants/{tenant_id}/atlassian/confluence/spaces",
    response_model=ConfluenceSpaceCatalogRead,
)
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


@router.get(
    "/tenants/{tenant_id}/atlassian/confluence/spaces/{space_key}/pages",
    response_model=list[ConfluencePageRead],
)
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
