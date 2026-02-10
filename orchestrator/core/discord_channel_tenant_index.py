from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from threading import Lock

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.core.discord_policy import channel_ids_from_discord_config
from orchestrator.storage.models import Project, Tenant


@dataclass(frozen=True)
class _DiscordChannelTenantIndex:
    revision: tuple[str, datetime | None, int, datetime | None, int]
    channel_to_tenant_ids: dict[str, set[str]]


_INDEX_LOCK = Lock()
_INDEX_CACHE: _DiscordChannelTenantIndex | None = None


def invalidate_discord_channel_tenant_index() -> None:
    global _INDEX_CACHE
    with _INDEX_LOCK:
        _INDEX_CACHE = None


def resolve_tenant_for_discord_channel(*, session: Session, channel_id: str) -> Tenant | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    tenant = _resolve_from_index(session=session, channel_id=normalized_channel_id, force_refresh=False)
    if tenant is not None:
        return tenant
    return _resolve_from_index(session=session, channel_id=normalized_channel_id, force_refresh=True)


def _resolve_from_index(*, session: Session, channel_id: str, force_refresh: bool) -> Tenant | None:
    index = _get_or_refresh_index(session=session, force_refresh=force_refresh)
    tenant_ids = index.channel_to_tenant_ids.get(channel_id, set())
    if len(tenant_ids) != 1:
        return None
    tenant_id = next(iter(tenant_ids))
    tenant = session.get(Tenant, tenant_id)
    if tenant is None or not tenant.is_enabled:
        return None
    if not _tenant_allows_channel(session=session, tenant_id=tenant_id, channel_id=channel_id):
        return None
    return tenant


def _get_or_refresh_index(*, session: Session, force_refresh: bool) -> _DiscordChannelTenantIndex:
    global _INDEX_CACHE
    revision = _current_revision(session=session)
    with _INDEX_LOCK:
        if not force_refresh and _INDEX_CACHE is not None and _INDEX_CACHE.revision == revision:
            return _INDEX_CACHE
        _INDEX_CACHE = _build_index(session=session, revision=revision)
        return _INDEX_CACHE


def _current_revision(*, session: Session) -> tuple[str, datetime | None, int, datetime | None, int]:
    bind_identity = str(session.bind.url) if session.bind is not None else "unknown"
    project_max_updated_at, project_count = session.execute(
        select(func.max(Project.updated_at), func.count(Project.project_id)).where(Project.is_archived.is_(False))
    ).one()
    tenant_max_updated_at, tenant_count = session.execute(
        select(func.max(Tenant.updated_at), func.count(Tenant.tenant_id)).where(Tenant.is_enabled.is_(True))
    ).one()
    return (bind_identity, project_max_updated_at, int(project_count), tenant_max_updated_at, int(tenant_count))


def _build_index(
    *,
    session: Session,
    revision: tuple[str, datetime | None, int, datetime | None, int],
) -> _DiscordChannelTenantIndex:
    channel_to_tenant_ids: dict[str, set[str]] = {}
    projects = session.execute(
        select(Project.tenant_id, Project.discord_config).where(Project.is_archived.is_(False))
    ).all()
    for tenant_id, discord_config in projects:
        for channel in channel_ids_from_discord_config(dict(discord_config or {})):
            channel_to_tenant_ids.setdefault(channel, set()).add(str(tenant_id))

    tenants = session.execute(
        select(Tenant.tenant_id, Tenant.discord_config).where(Tenant.is_enabled.is_(True))
    ).all()
    for tenant_id, discord_config in tenants:
        channel = str((discord_config or {}).get("channel_id") or "").strip()
        if channel:
            channel_to_tenant_ids.setdefault(channel, set()).add(str(tenant_id))

    return _DiscordChannelTenantIndex(
        revision=revision,
        channel_to_tenant_ids=channel_to_tenant_ids,
    )


def _tenant_allows_channel(*, session: Session, tenant_id: str, channel_id: str) -> bool:
    projects = session.execute(
        select(Project.discord_config).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).all()
    for (discord_config,) in projects:
        if channel_id in channel_ids_from_discord_config(dict(discord_config or {})):
            return True
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        return False
    tenant_channel = str((tenant.discord_config or {}).get("channel_id") or "").strip()
    return channel_id == tenant_channel
