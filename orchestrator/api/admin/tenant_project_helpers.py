from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    PLATFORM_SECRET_DISCORD_GUILD_ID_REF,
    resolve_platform_secret_ref,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordTextChannel


def slugify_tenant_name(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "tenant"


def allocate_tenant_id(session: Session, *, name: str) -> str:
    base = slugify_tenant_name(name)
    candidate = base
    suffix = 2
    while session.get(Tenant, candidate) is not None:
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def primary_jira_project_key(jira_config: dict) -> str | None:
    project_keys = jira_config.get("project_keys")
    if isinstance(project_keys, list):
        for key in project_keys:
            key_normalized = str(key).strip().upper()
            if key_normalized:
                return key_normalized
    return None


def primary_repo_url(repos_config: dict) -> str | None:
    candidates: list[object] = [
        repos_config.get("github_repository"),
        repos_config.get("fallback_repo"),
    ]

    allowlist = repos_config.get("allowlist")
    if isinstance(allowlist, list):
        candidates.extend(allowlist)

    by_project = repos_config.get("mapping_rules_by_project_key")
    if isinstance(by_project, dict):
        candidates.extend(by_project.values())

    by_component = repos_config.get("mapping_rules_by_component")
    if isinstance(by_component, dict):
        candidates.extend(by_component.values())

    for candidate in candidates:
        if candidate is None:
            continue
        normalized = str(candidate).strip()
        if normalized and normalized.lower() != "none":
            return normalized
    return None


def resolve_project_discord_channel_binding(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    discord_config: dict,
    resolve_project_discord_channel_name_fn,
) -> dict:
    normalized = dict(discord_config or {})
    existing_channel_id = str(normalized.get("channel_id") or "").strip()
    if existing_channel_id:
        normalized["channel_id"] = existing_channel_id
        return normalized

    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise ValueError(f"Discord bot token secret is missing: {token_ref}")

    guild_id = settings.discord_guild_id.strip()
    if not guild_id:
        guild_id = (
            resolve_platform_secret_ref(
                session,
                secret_ref=PLATFORM_SECRET_DISCORD_GUILD_ID_REF,
                encryption_key=settings.secrets_encryption_key,
            )
            or ""
        ).strip()
    if not guild_id:
        raise ValueError("Discord guild ID is not configured")

    parent_id = settings.discord_channel_category_id.strip() or None
    channel_name = resolve_project_discord_channel_name_fn(settings=settings, tenant=tenant, project=project)
    client = DiscordApiClient(bot_token=bot_token)
    channel: DiscordTextChannel = client.ensure_text_channel(
        guild_id=guild_id,
        name=channel_name,
        parent_id=parent_id,
    )
    normalized["channel_id"] = channel.channel_id
    return normalized


def ensure_default_project_for_tenant(
    session: Session,
    *,
    tenant: Tenant,
    default_project_name_from_repo_fn,
) -> None:
    existing = session.execute(
        select(Project).where(Project.tenant_id == tenant.tenant_id).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return

    repo_url = primary_repo_url(dict(tenant.repos_config))
    jira_project_key = primary_jira_project_key(dict(tenant.jira_config))
    if not repo_url or not jira_project_key:
        return

    now = datetime.now(timezone.utc)
    session.add(
        Project(
            project_id=f"{tenant.tenant_id}-default",
            tenant_id=tenant.tenant_id,
            name=default_project_name_from_repo_fn(repo_url=repo_url, tenant_id=tenant.tenant_id),
            github_repository=repo_url,
            jira_project_key=jira_project_key,
            policy_overrides={},
            environment={},
            secret_refs={},
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
    )


def sync_tenant_jira_project_keys(
    session: Session,
    *,
    tenant: Tenant,
    normalize_project_key_fn,
) -> None:
    persisted_projects = session.execute(
        select(Project)
        .where(Project.tenant_id == tenant.tenant_id, Project.is_archived.is_(False))
        .order_by(Project.created_at.asc())
    ).scalars().all()
    pending_projects = [
        project
        for project in session.new
        if isinstance(project, Project)
        and project.tenant_id == tenant.tenant_id
        and not project.is_archived
    ]
    persisted_ids = {item.project_id for item in persisted_projects}
    projects = persisted_projects + [project for project in pending_projects if project.project_id not in persisted_ids]
    keys: list[str] = []
    for project in projects:
        if project.is_archived:
            continue
        normalized = normalize_project_key_fn(project.jira_project_key)
        if normalized and normalized not in keys:
            keys.append(normalized)
    jira_config = dict(tenant.jira_config)
    jira_config["project_keys"] = keys
    tenant.jira_config = jira_config
