from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.jira_oauth.service import jira_oauth_client, refresh_jira_connection_tokens
from orchestrator.storage.models import JiraOAuthConnection, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthClient


@dataclass(frozen=True)
class JiraTenantOAuthContext:
    connection: JiraOAuthConnection
    client: JiraOAuthClient
    access_token: str


def resolve_tenant_jira_connection(*, session: Session, tenant: Tenant) -> JiraOAuthConnection:
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira OAuth connection is not linked for this tenant",
        )
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Configured Jira connection was not found",
        )
    return connection


def tenant_jira_oauth_context(
    *,
    session: Session,
    tenant: Tenant,
    settings,
) -> JiraTenantOAuthContext:  # noqa: ANN001
    connection = resolve_tenant_jira_connection(session=session, tenant=tenant)
    access_token = refresh_jira_connection_tokens(
        session,
        connection=connection,
        settings=settings,
    )
    client = jira_oauth_client(session=session, settings=settings)
    return JiraTenantOAuthContext(
        connection=connection,
        client=client,
        access_token=access_token,
    )
