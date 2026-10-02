from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.discord.ask.context import (
    fetch_jira_issue_detail_for_tenant,
    fetch_jira_issue_preview_for_tenant,
)
from orchestrator.api.atlassian_oauth.connection_service import (
    resolve_tenant_atlassian_connection,
)
from orchestrator.api.atlassian_oauth.service import (
    atlassian_oauth_client,
    refresh_atlassian_connection_tokens,
)
from orchestrator.tools.atlassian_oauth import JiraIssuePreview


def tenant_atlassian_oauth_context(*, session: Session, tenant, settings):  # noqa: ANN001
    connection = resolve_tenant_atlassian_connection(session=session, tenant=tenant)
    access_token = refresh_atlassian_connection_tokens(
        session,
        connection=connection,
        settings=settings,
    )
    client = atlassian_oauth_client(session=session, settings=settings)
    return {"connection": connection, "access_token": access_token, "client": client}


def fetch_jira_issue_preview(
    *, session: Session, tenant, issue_key: str
) -> JiraIssuePreview:  # noqa: ANN001
    return fetch_jira_issue_preview_for_tenant(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
    )


def fetch_jira_issue_detail(*, session: Session, tenant, issue_key: str):  # noqa: ANN001
    return fetch_jira_issue_detail_for_tenant(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
    )
