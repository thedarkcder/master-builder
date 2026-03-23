from __future__ import annotations

import re

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.storage.models import Project, Tenant

DM_SCOPE_SENTINEL_CHANNEL_IDS = {"__dm__", "__dm", "dm"}
PROJECT_SCOPED_COMMANDS = {"ask", "pm", "bug", "gap", "issues", "run", "retry", "reply", "link"}
_ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


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


def _tenant_active_projects(*, session, tenant_id: str) -> list[Project]:  # noqa: ANN001
    return session.execute(
        select(Project)
        .where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
        .order_by(Project.created_at)
    ).scalars().all()


def _resolve_single_project_for_scope(*, session, tenant: Tenant, scope: CommandScope) -> Project | None:  # noqa: ANN001
    normalized_scope_keys = {str(value).strip().upper() for value in scope.project_keys if str(value).strip()}
    if len(normalized_scope_keys) != 1:
        return None
    target_project_key = next(iter(normalized_scope_keys))
    projects = _tenant_active_projects(session=session, tenant_id=tenant.tenant_id)
    for project in projects:
        if str(project.jira_project_key or "").strip().upper() == target_project_key:
            return project
    return None


def _candidate_issue_key_for_command(
    *,
    command_name: str,
    arguments: tuple[str, ...],
    payload,
) -> str | None:  # noqa: ANN001
    if command_name in {"run", "reply", "link", "gap"} and arguments:
        candidate = str(arguments[0]).strip().upper()
        return candidate if _ISSUE_KEY_PATTERN.match(candidate) else None
    if command_name == "retry" and arguments:
        candidate = str(arguments[0]).strip().upper()
        return candidate if _ISSUE_KEY_PATTERN.match(candidate) else None
    if command_name in {"ask", "pm"} and arguments:
        first_token = str(arguments[0]).strip()
        if first_token.startswith("@"):
            candidate = first_token[1:].strip().upper()
            return candidate if _ISSUE_KEY_PATTERN.match(candidate) else None
    if command_name == "bug" and isinstance(getattr(payload, "command_params", None), dict):
        candidate = str(payload.command_params.get("issue_key") or "").strip().upper()
        return candidate if _ISSUE_KEY_PATTERN.match(candidate) else None
    return None


def enrich_command_scope(
    *,
    session,
    tenant: Tenant,
    command_name: str,
    arguments: tuple[str, ...],
    payload,
    current_scope: CommandScope,
    find_active_project_for_issue_key_fn,
) -> CommandScope:  # noqa: ANN001
    if command_name not in PROJECT_SCOPED_COMMANDS:
        return current_scope
    if current_scope.project_id:
        return current_scope

    issue_key = _candidate_issue_key_for_command(
        command_name=command_name,
        arguments=arguments,
        payload=payload,
    )
    if issue_key:
        project = find_active_project_for_issue_key_fn(
            session,
            tenant_id=tenant.tenant_id,
            issue_key=issue_key,
        )
        if project is not None:
            return CommandScope(
                project_id=project.project_id,
                project_keys=(project.jira_project_key,),
                channel_id=current_scope.channel_id,
            )

    project = _resolve_single_project_for_scope(session=session, tenant=tenant, scope=current_scope)
    if project is not None:
        return CommandScope(
            project_id=project.project_id,
            project_keys=(project.jira_project_key,),
            channel_id=current_scope.channel_id,
        )

    if command_name in {"ask", "pm", "bug", "issues"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Command '{command_name}' requires a single mapped project scope",
        )
    return current_scope


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
