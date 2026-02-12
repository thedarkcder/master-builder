from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.storage.models import JiraOAuthConnection, Tenant


def tenant_jira_browse_base_url(*, session: Session, tenant: Tenant) -> str | None:
    connection_id = str((tenant.jira_config or {}).get("connection_id") or "").strip()
    if not connection_id:
        return None
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        return None
    site_url = str(connection.site_url or "").strip().rstrip("/")
    return site_url or None


def tenant_jira_issue_url(*, session: Session, tenant: Tenant, issue_key: str | None) -> str | None:
    normalized_issue_key = str(issue_key or "").strip().upper()
    if not normalized_issue_key:
        return None
    base_url = tenant_jira_browse_base_url(session=session, tenant=tenant)
    if not base_url:
        return None
    return f"{base_url}/browse/{normalized_issue_key}"
