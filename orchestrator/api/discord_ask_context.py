from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.routes_discord import (
    _project_filter_jql as _routes_project_filter_jql,
    _search_jira_issues_for_tenant as _routes_search_jira_issues_for_tenant,
    consume_pending_ask_action as _routes_consume_pending_ask_action,
    remove_issue_key_from_tenant_ask_history as _routes_remove_issue_key_from_tenant_ask_history,
)
from orchestrator.storage.models import Tenant
from orchestrator.tools.jira_oauth import JiraIssuePreview


def project_filter_jql(*, session: Session, tenant: Tenant, channel_id: str | None = None) -> str:
    return _routes_project_filter_jql(session=session, tenant=tenant, channel_id=channel_id)


def search_jira_issues_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    jql: str,
    max_results: int = 20,
) -> list[JiraIssuePreview]:
    return _routes_search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=jql,
        max_results=max_results,
    )


def consume_pending_ask_action(*, session: Session, tenant: Tenant, request_id: str) -> dict | None:
    return _routes_consume_pending_ask_action(session=session, tenant=tenant, request_id=request_id)


def remove_issue_key_from_tenant_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    user_id: str | None = None,
    channel_id: str | None = None,
) -> int:
    return _routes_remove_issue_key_from_tenant_ask_history(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        user_id=user_id,
        channel_id=channel_id,
    )
