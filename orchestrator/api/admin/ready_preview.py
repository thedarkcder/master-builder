from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.schemas import ReadyGatePreviewRead, ReadyIssuePreviewRead
from orchestrator.storage.models import JiraOAuthConnection, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError


def preview_tenant_ready_gate(
    *,
    session,
    tenant_id: str,
    max_results: int,
    settings,
    default_ready_jql_fn,
    refresh_jira_connection_tokens_fn,
    jira_oauth_client_fn,
) -> ReadyGatePreviewRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    jira_config = tenant.jira_config
    project_keys = jira_config.get("project_keys")
    if not isinstance(project_keys, list) or not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Jira project_keys")

    raw_ready_statuses = jira_config.get("ready_statuses")
    if isinstance(raw_ready_statuses, list):
        ready_statuses = [str(value).strip() for value in raw_ready_statuses if str(value).strip()]
    else:
        ready_statuses = []
    if not ready_statuses:
        ready_statuses = ["Ready for Agent"]

    raw_ready_jql = jira_config.get("ready_jql")
    ready_jql = raw_ready_jql.strip() if isinstance(raw_ready_jql, str) else ""
    if not ready_jql:
        ready_jql = default_ready_jql_fn(project_keys=project_keys, ready_statuses=ready_statuses)

    connection_id = jira_config.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Jira OAuth connection is not linked")
    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configured Jira connection was not found")

    try:
        access_token = refresh_jira_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant_id,
        )
        client = jira_oauth_client_fn(session=session, settings=settings, tenant_id=tenant_id)
        issues = client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=ready_jql,
            max_results=max_results,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Ready preview failed: {exc}") from exc

    guidance = (
        "Issues are executable only when they are in a configured ready status. "
        "If an issue is missing here, move it to a ready status and retry."
    )
    return ReadyGatePreviewRead(
        ready_statuses=ready_statuses,
        ready_jql=ready_jql,
        eligible_issues=[
            ReadyIssuePreviewRead(key=issue.key, summary=issue.summary, status=issue.status)
            for issue in issues
        ],
        guidance=guidance,
    )
