from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.decision.types import jira_config_project_keys
from orchestrator.core.platform.secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import (
    DiscordApiClient,
    DiscordCategoryChannel,
    DiscordTextChannel,
    DiscordVoiceChannel,
)
from orchestrator.tools.discord_api import DiscordApiError


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


def allocate_project_id(session: Session) -> str:
    while True:
        candidate = f"proj_{uuid4().hex[:16]}"
        if session.get(Project, candidate) is None:
            return candidate


def primary_jira_project_key(jira_config: dict) -> str | None:
    for key in jira_config_project_keys(jira_config=jira_config):
        key_normalized = key.upper()
        if key_normalized:
            return key_normalized
    return None


def primary_repo_url(repos_config: dict) -> str | None:
    candidates: list[object] = [
        repos_config.get("github_repository"),
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
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise ValueError(f"Discord bot token secret is missing: {token_ref}")

    tenant_discord_config = getattr(tenant, "discord_config", None) or {}
    guild_id = str(tenant_discord_config.get("guild_id") or "").strip()
    if not guild_id:
        raise ValueError("Discord guild ID is not configured")

    parent_id = settings.discord_channel_category_id.strip() or None
    channel_name = resolve_project_discord_channel_name_fn(
        settings=settings, tenant=tenant, project=project
    )
    client = DiscordApiClient(bot_token=bot_token)
    existing_channel_id = str(normalized.get("channel_id") or "").strip()
    if existing_channel_id and not _discord_channel_exists(
        client=client, channel_id=existing_channel_id
    ):
        existing_channel_id = ""
        normalized.pop("channel_id", None)
    if not existing_channel_id:
        channel: DiscordTextChannel = client.ensure_text_channel(
            guild_id=guild_id,
            name=channel_name,
            parent_id=parent_id,
        )
        existing_channel_id = channel.channel_id
    normalized["channel_id"] = existing_channel_id

    live_voice_links: dict[str, str] = {}
    for voice_channel_id, linked_channel_id in dict(
        normalized.get("live_voice_room_links") or {}
    ).items():
        normalized_voice_channel_id = str(voice_channel_id).strip()
        normalized_linked_channel_id = str(linked_channel_id).strip()
        if not normalized_voice_channel_id or not normalized_linked_channel_id:
            continue
        if not _discord_channel_exists(
            client=client, channel_id=normalized_voice_channel_id
        ):
            continue
        if (
            normalized_linked_channel_id != existing_channel_id
            and not _discord_channel_exists(
                client=client,
                channel_id=normalized_linked_channel_id,
            )
        ):
            normalized_linked_channel_id = existing_channel_id
        live_voice_links[normalized_voice_channel_id] = normalized_linked_channel_id

    if not live_voice_links:
        voice_parent_id = _resolve_voice_channel_parent_id(
            client=client,
            guild_id=guild_id,
            fallback_parent_id=parent_id,
        )
        voice_channel: DiscordVoiceChannel = client.ensure_voice_channel(
            guild_id=guild_id,
            name=f"{channel_name}-voice",
            parent_id=voice_parent_id,
        )
        live_voice_links = {voice_channel.channel_id: existing_channel_id}
        normalized["live_voice_enabled"] = True

    normalized["live_voice_room_links"] = live_voice_links
    voice_room_channel_ids = [
        channel_id
        for channel_id in [
            str(value).strip() for value in normalized.get("voice_room_channel_ids", [])
        ]
        if channel_id
    ]
    for voice_channel_id in live_voice_links:
        if voice_channel_id not in voice_room_channel_ids:
            voice_room_channel_ids.append(voice_channel_id)
    if voice_room_channel_ids:
        normalized["voice_room_channel_ids"] = voice_room_channel_ids
        normalized["voice_room_channel_id"] = voice_room_channel_ids[0]
    return normalized


def _discord_channel_exists(*, client: DiscordApiClient, channel_id: str) -> bool:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return False
    try:
        channel = client.get_channel(channel_id=normalized_channel_id)
    except (DiscordApiError, ValueError):
        return False
    return str(channel.get("id") or "").strip() == normalized_channel_id


def _resolve_voice_channel_parent_id(
    *,
    client: DiscordApiClient,
    guild_id: str,
    fallback_parent_id: str | None,
) -> str | None:
    categories = client.list_channel_categories(guild_id=guild_id)
    if not categories:
        return fallback_parent_id

    voice_channels = client.list_voice_channels(guild_id=guild_id)
    voice_parent_counts = Counter(
        channel.parent_id for channel in voice_channels if channel.parent_id
    )
    voice_named_categories = [
        category
        for category in categories
        if _looks_like_voice_category_name(category.name)
    ]
    candidates = [
        category
        for category in voice_named_categories
        if voice_parent_counts.get(category.channel_id, 0) > 0
    ]
    if candidates:
        return _select_preferred_category_id(
            candidates=candidates, voice_parent_counts=voice_parent_counts
        )
    if voice_named_categories:
        return _select_preferred_category_id(
            candidates=voice_named_categories, voice_parent_counts=voice_parent_counts
        )
    voice_backed_categories = [
        category
        for category in categories
        if voice_parent_counts.get(category.channel_id, 0) > 0
    ]
    if voice_backed_categories:
        return _select_preferred_category_id(
            candidates=voice_backed_categories, voice_parent_counts=voice_parent_counts
        )
    return fallback_parent_id


def _looks_like_voice_category_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", " ", name.strip().lower())
    tokens = {token for token in normalized.split() if token}
    return "voice" in tokens or {"live", "voice"}.issubset(tokens)


def _select_preferred_category_id(
    *,
    candidates: list[DiscordCategoryChannel],
    voice_parent_counts: Counter[str],
) -> str:
    selected = sorted(
        candidates,
        key=lambda category: (
            -voice_parent_counts.get(category.channel_id, 0),
            category.name.lower(),
            category.channel_id,
        ),
    )[0]
    return selected.channel_id


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
            project_id=allocate_project_id(session),
            tenant_id=tenant.tenant_id,
            name=default_project_name_from_repo_fn(
                repo_url=repo_url, tenant_id=tenant.tenant_id
            ),
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


def sync_tenant_project_discord_channels(
    session: Session,
    *,
    tenant: Tenant,
    settings,  # noqa: ANN001
    resolve_project_discord_channel_binding_fn,
) -> None:
    tenant_discord_config = dict(getattr(tenant, "discord_config", None) or {})
    guild_id = str(tenant_discord_config.get("guild_id") or "").strip()
    if not guild_id:
        return

    persisted_projects = (
        session.execute(
            select(Project)
            .where(
                Project.tenant_id == tenant.tenant_id, Project.is_archived.is_(False)
            )
            .order_by(Project.created_at.asc())
        )
        .scalars()
        .all()
    )
    pending_projects = [
        project
        for project in session.new
        if isinstance(project, Project)
        and project.tenant_id == tenant.tenant_id
        and not project.is_archived
    ]
    persisted_ids = {item.project_id for item in persisted_projects}
    projects = persisted_projects + [
        project
        for project in pending_projects
        if project.project_id not in persisted_ids
    ]
    if not projects:
        return

    now = datetime.now(timezone.utc)
    updated_any = False
    for project in projects:
        discord_config = dict(project.discord_config or {})
        updated_config = resolve_project_discord_channel_binding_fn(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            discord_config=discord_config,
        )
        if updated_config == discord_config:
            continue
        project.discord_config = updated_config
        project.updated_at = now
        updated_any = True

    if updated_any:
        tenant.updated_at = now


def sync_tenant_jira_project_keys(
    session: Session,
    *,
    tenant: Tenant,
    normalize_project_key_fn,
) -> None:
    persisted_projects = (
        session.execute(
            select(Project)
            .where(
                Project.tenant_id == tenant.tenant_id, Project.is_archived.is_(False)
            )
            .order_by(Project.created_at.asc())
        )
        .scalars()
        .all()
    )
    pending_projects = [
        project
        for project in session.new
        if isinstance(project, Project)
        and project.tenant_id == tenant.tenant_id
        and not project.is_archived
    ]
    persisted_ids = {item.project_id for item in persisted_projects}
    projects = persisted_projects + [
        project
        for project in pending_projects
        if project.project_id not in persisted_ids
    ]
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
