from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.discord_ask_history_service import DiscordAskHistoryService
from orchestrator.api.discord_channel_scope_repository import SqlAlchemyDiscordChannelScopeRepository
from orchestrator.api.jira_oauth_service import jira_oauth_client as _jira_oauth_client
from orchestrator.api.jira_oauth_service import refresh_jira_connection_tokens as _refresh_jira_connection_tokens
from orchestrator.core.config import get_settings
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.tools.jira_oauth import JiraIssueDetail, JiraIssuePreview, JiraOAuthError

_channel_scope_repository = SqlAlchemyDiscordChannelScopeRepository()
_ask_history_service = DiscordAskHistoryService(
    max_pending_actions=50,
    max_history_entries=80,
    max_history_context=6,
)
_DM_SCOPE_SENTINEL_CHANNEL_IDS = {"__dm__", "__dm", "dm"}


def _normalize_scope_channel_id(channel_id: str | None) -> str | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    if normalized_channel_id.lower() in _DM_SCOPE_SENTINEL_CHANNEL_IDS:
        return None
    if normalized_channel_id.lower().startswith("jira:"):
        return None
    return normalized_channel_id


def tenant_active_projects(*, session: Session, tenant_id: str) -> list[Project]:
    return session.execute(
        select(Project)
        .where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
        .order_by(Project.created_at)
    ).scalars().all()


def tenant_project_keys(*, session: Session, tenant: Tenant) -> list[str]:
    keys = [project.jira_project_key for project in tenant_active_projects(session=session, tenant_id=tenant.tenant_id)]
    if keys:
        return keys
    return [str(key).strip().upper() for key in tenant.jira_config.get("project_keys", []) if str(key).strip()]


def project_filter_jql(*, session: Session, tenant: Tenant, channel_id: str | None = None) -> str:
    normalized_channel_id = _normalize_scope_channel_id(channel_id)
    if normalized_channel_id:
        scope = _channel_scope_repository.resolve_project_scope(
            session=session,
            tenant=tenant,
            channel_id=normalized_channel_id,
        )
        if scope is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Discord channel is not mapped to an active project",
            )
        return f'project = "{scope.jira_project_key}"'
    keys = tenant_project_keys(session=session, tenant=tenant)
    if not keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")
    if len(keys) == 1:
        return f'project = "{keys[0]}"'
    joined = ", ".join(f'"{key}"' for key in keys)
    return f"project in ({joined})"


def search_jira_issues_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    jql: str,
    max_results: int = 20,
) -> list[JiraIssuePreview]:
    settings = get_settings()
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
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
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        return client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            jql=jql,
            max_results=max_results,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Jira board: {exc}",
        ) from exc


def fetch_jira_issue_preview_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> JiraIssuePreview:
    issues = search_jira_issues_for_tenant(
        session=session,
        tenant=tenant,
        jql=f'key = "{issue_key}"',
        max_results=1,
    )
    if not issues:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Issue {issue_key} was not found in Jira",
        )
    return issues[0]


def fetch_jira_issue_detail_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
) -> JiraIssueDetail:
    settings = get_settings()
    connection_id = str(tenant.jira_config.get("connection_id") or "").strip()
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
    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
        )
        client = _jira_oauth_client(session=session, settings=settings)
        return client.get_issue_detail(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to query Jira issue details: {exc}",
        ) from exc


def consume_pending_ask_action(*, session: Session, tenant: Tenant, request_id: str) -> dict | None:
    return _ask_history_service.consume_pending_ask_action(
        session=session,
        tenant=tenant,
        request_id=request_id,
    )


def remove_issue_key_from_tenant_ask_history(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str,
    user_id: str | None = None,
    channel_id: str | None = None,
) -> int:
    return _ask_history_service.remove_issue_key_from_ask_history(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        user_id=user_id,
        channel_id=channel_id,
    )
