from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.schemas import ReadyGatePreviewRead, ReadyIssuePreviewRead
from orchestrator.core.platform.operational_health_service import (
    tenant_integration_snapshot,
)
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError


def preview_tenant_ready_gate(
    *,
    session,
    tenant_id: str,
    max_results: int,
    settings,
    default_ready_jql_fn,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
) -> ReadyGatePreviewRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )

    integration = tenant_integration_snapshot(tenant=tenant)
    project_keys = list(integration.jira_project_keys)
    if not project_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Missing Jira project_keys"
        )

    ready_statuses = list(integration.jira_ready_statuses)
    if not ready_statuses:
        ready_statuses = ["Ready for Agent"]

    jira_config = tenant.jira_config
    raw_ready_jql = jira_config.get("ready_jql")
    ready_jql = raw_ready_jql.strip() if isinstance(raw_ready_jql, str) else ""
    if not ready_jql:
        ready_jql = default_ready_jql_fn(
            project_keys=project_keys, ready_statuses=ready_statuses
        )

    connection_id = integration.atlassian_connection_id
    if not connection_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Atlassian connection is not linked",
        )
    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Configured Atlassian connection was not found",
        )

    try:
        access_token = refresh_atlassian_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant_id,
        )
        client = atlassian_oauth_client_fn(
            session=session, settings=settings, tenant_id=tenant_id
        )
        issues = client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=ready_jql,
            max_results=max_results,
        )
    except (ValueError, AtlassianOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Ready preview failed: {exc}",
        ) from exc

    guidance = (
        "Issues are executable only when they are in a configured ready status. "
        "If an issue is missing here, move it to a ready status and retry."
    )
    return ReadyGatePreviewRead(
        ready_statuses=ready_statuses,
        ready_jql=ready_jql,
        eligible_issues=[
            ReadyIssuePreviewRead(
                key=issue.key, summary=issue.summary, status=issue.status
            )
            for issue in issues
        ],
        guidance=guidance,
    )
