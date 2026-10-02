from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.jira_route_service import (
    list_jira_projects_for_connection as list_jira_projects_for_connection_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import JiraProjectRead
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
from orchestrator.storage.models import AtlassianOAuthConnection

router = APIRouter(prefix="/api/admin", tags=["admin", "atlassian-jira"])


@router.get(
    "/atlassian/connections/{connection_id}/jira-projects",
    response_model=list[JiraProjectRead],
)
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
