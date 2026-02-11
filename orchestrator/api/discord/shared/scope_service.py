from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.storage.models import Project, Tenant

DM_SCOPE_SENTINEL_CHANNEL_IDS = {"__dm__", "__dm", "dm"}


def normalize_scope_channel_id(channel_id: str | None) -> str | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    if normalized_channel_id.lower() in DM_SCOPE_SENTINEL_CHANNEL_IDS:
        return None
    return normalized_channel_id


def resolve_command_scope(
    *,
    session,
    tenant: Tenant,
    channel_id: str | None,
    channel_scope_repository,
    tenant_project_keys_fn,
) -> CommandScope:  # noqa: ANN001
    normalized_channel_id = normalize_scope_channel_id(channel_id)
    if normalized_channel_id:
        scope = channel_scope_repository.resolve_project_scope(
            session=session,
            tenant=tenant,
            channel_id=normalized_channel_id,
        )
        if scope is not None and scope.jira_project_key:
            return CommandScope(
                project_id=scope.project_id,
                project_keys=(scope.jira_project_key,),
                channel_id=normalized_channel_id,
            )
    return CommandScope(
        project_keys=tuple(tenant_project_keys_fn(session=session, tenant=tenant)),
        channel_id=normalized_channel_id,
    )


def resolve_project_for_issue(
    *,
    session,
    tenant: Tenant,
    issue_key: str,
    find_active_project_for_issue_key_fn,
) -> Project:  # noqa: ANN001
    project = find_active_project_for_issue_key_fn(
        session,
        tenant_id=tenant.tenant_id,
        issue_key=issue_key,
    )
    if project is not None:
        return project
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"No active project mapping found for issue {issue_key}",
    )
