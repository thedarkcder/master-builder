from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from orchestrator.core.discord_policy import channel_ids_from_discord_config
from orchestrator.storage.models import Project, Tenant


def _tenant_primary_channel_id(tenant: Tenant) -> str | None:
    discord_config = dict(tenant.discord_config or {})
    normalized_channel_id = str(discord_config.get("channel_id") or "").strip()
    return normalized_channel_id or None


def resolve_project_for_discord_channel(*, session, tenant_id: str, channel_id: str | None) -> Project | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    for project in projects:
        if normalized_channel_id in channel_ids_from_discord_config(dict(project.discord_config or {})):
            return project
    tenant = session.get(Tenant, tenant_id)
    if tenant is not None:
        tenant_channel_id = _tenant_primary_channel_id(tenant)
        if tenant_channel_id and normalized_channel_id == tenant_channel_id and len(projects) == 1:
            return projects[0]
    return None


def project_allowed_channel_ids(*, session, tenant_id: str) -> set[str]:
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    allowed: set[str] = set()
    for project in projects:
        allowed.update(channel_ids_from_discord_config(dict(project.discord_config or {})))
    return allowed


def tenant_allowed_channel_ids(*, session, tenant: Tenant) -> set[str]:
    allowed = project_allowed_channel_ids(session=session, tenant_id=tenant.tenant_id)
    tenant_channel_id = _tenant_primary_channel_id(tenant)
    if tenant_channel_id:
        allowed.add(tenant_channel_id)
    return allowed


def save_project_allowlist_requests(*, session, tenant: Tenant, project: Project, requests: list[dict]) -> None:
    discord_config = dict(project.discord_config or {})
    discord_config["allowlist_requests"] = requests
    project.discord_config = discord_config
    project.updated_at = datetime.now(timezone.utc)
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()


def save_seed_followups(
    *,
    session,
    tenant: Tenant,
    entries: list[dict],
    removed_channel_ids: set[str] | None = None,
) -> None:
    discord_config = dict(tenant.discord_config or {})
    discord_config["seed_followups"] = entries
    if removed_channel_ids:
        raw_seed_thread_ids = discord_config.get("seed_followup_thread_channel_ids")
        seed_thread_ids = (
            [str(value).strip() for value in raw_seed_thread_ids if str(value).strip()]
            if isinstance(raw_seed_thread_ids, list)
            else []
        )
        seed_thread_ids = [value for value in seed_thread_ids if value not in removed_channel_ids]
        discord_config["seed_followup_thread_channel_ids"] = seed_thread_ids
    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
