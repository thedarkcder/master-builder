from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.atlassian_oauth.service import atlassian_oauth_client, refresh_atlassian_connection_tokens
from orchestrator.core.decision_types import JiraConfigKey, tenant_jira_config_text
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthClient


@dataclass(frozen=True)
class AtlassianTenantOAuthContext:
    connection: AtlassianOAuthConnection
    client: AtlassianOAuthClient
    access_token: str


def resolve_tenant_atlassian_connection(*, session: Session, tenant: Tenant) -> AtlassianOAuthConnection:
    connection_id = tenant_jira_config_text(tenant=tenant, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Atlassian connection is not linked for this tenant",
        )
    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Atlassian connection was not found",
        )
    return connection


def tenant_atlassian_oauth_context(
    *,
    session: Session,
    tenant: Tenant,
    settings,
) -> AtlassianTenantOAuthContext:  # noqa: ANN001
    connection = resolve_tenant_atlassian_connection(session=session, tenant=tenant)
    access_token = refresh_atlassian_connection_tokens(
        session,
        connection=connection,
        settings=settings,
    )
    client = atlassian_oauth_client(session=session, settings=settings)
    return AtlassianTenantOAuthContext(
        connection=connection,
        client=client,
        access_token=access_token,
    )
