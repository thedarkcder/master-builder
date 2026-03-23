from __future__ import annotations

import logging
from re import Pattern

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.discord.shared.state_repository import resolve_project_for_discord_channel
from orchestrator.core.decision_reply_service import active_case_and_cycle_for_issue
from orchestrator.core.discord.channel_tenant_index import resolve_tenant_for_discord_channel
from orchestrator.core.discord.thread_context import get_thread_issue_key, remove_thread_issue_key
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

logger = logging.getLogger(__name__)


def tenant_discord_channel_ids(*, tenant: Tenant, project_channel_ids: set[str]) -> set[str]:
    discord_config = tenant.discord_config or {}
    channel_ids: set[str] = set(project_channel_ids)
    configured_channel_id = str(discord_config.get("channel_id") or "").strip()
    if configured_channel_id:
        channel_ids.add(configured_channel_id)
    return channel_ids


def resolve_project_for_channel(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str,
) -> Project | None:
    return resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=channel_id,
    )


def project_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    channel_ids: set[str] = set()
    for project in projects:
        discord_config = dict(project.discord_config or {})
        configured_channel_id = str(discord_config.get("channel_id") or "").strip()
        if configured_channel_id:
            channel_ids.add(configured_channel_id)
        raw_thread_ids = discord_config.get("ask_thread_channel_ids")
        if isinstance(raw_thread_ids, list):
            for value in raw_thread_ids:
                normalized = str(value or "").strip()
                if normalized:
                    channel_ids.add(normalized)
        raw_seed_thread_ids = discord_config.get("seed_followup_thread_channel_ids")
        if isinstance(raw_seed_thread_ids, list):
            for value in raw_seed_thread_ids:
                normalized = str(value or "").strip()
                if normalized:
                    channel_ids.add(normalized)
    return channel_ids


def project_ask_thread_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    return _project_thread_channel_ids_for_tenant(
        session=session,
        tenant_id=tenant_id,
        config_key="ask_thread_channel_ids",
    )


def project_seed_followup_thread_channel_ids_for_tenant(*, session: Session, tenant_id: str) -> set[str]:
    return _project_thread_channel_ids_for_tenant(
        session=session,
        tenant_id=tenant_id,
        config_key="seed_followup_thread_channel_ids",
    )


def ask_thread_message_map_from_config(discord_config: dict) -> dict[str, str]:
    raw_map = discord_config.get("ask_thread_by_message_id")
    if not isinstance(raw_map, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw_map.items():
        message_id = str(key or "").strip()
        thread_id = str(value or "").strip()
        if message_id and thread_id:
            normalized[message_id] = thread_id
    return normalized


def resolve_thread_channel_for_reply(
    *,
    session: Session,
    channel_id: str,
    reply_to_message_id: str,
) -> str:
    normalized_channel_id = str(channel_id or "").strip()
    normalized_reply_to_message_id = str(reply_to_message_id or "").strip()
    if not normalized_channel_id or not normalized_reply_to_message_id:
        return normalized_channel_id
    tenant = resolve_tenant_for_discord_channel(session=session, channel_id=normalized_channel_id)
    if tenant is None:
        return normalized_channel_id
    project = resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=normalized_channel_id,
    )
    if project is None:
        return normalized_channel_id
    ask_thread_message_map = ask_thread_message_map_from_config(project.discord_config or {})
    mapped_thread_channel_id = str(ask_thread_message_map.get(normalized_reply_to_message_id) or "").strip()
    return mapped_thread_channel_id or normalized_channel_id


def resolve_thread_id_by_message_suffix(
    *,
    client: DiscordApiClient,
    thread_ids: list[str],
    message_id: str,
) -> str | None:
    suffix = message_id.strip()[-6:]
    if not suffix:
        return None
    for thread_id in thread_ids:
        try:
            channel = client.get_channel(channel_id=thread_id)
        except (DiscordApiError, ValueError) as exc:
            logger.exception(
                "discord_thread_lookup_failed thread_id=%s message_id=%s error=%s",
                thread_id,
                message_id,
                exc,
            )
            continue
        channel_name = str(channel.get("name") or "").strip()
        if channel_name.endswith(suffix):
            return thread_id
    return None


def decision_gate_issue_for_thread(
    *,
    session: Session,
    channel_id: str,
    issue_key_pattern: Pattern[str],
) -> tuple[str, str] | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    tenant = resolve_tenant_for_discord_channel(session=session, channel_id=normalized_channel_id)
    if tenant is None:
        return None
    project = resolve_project_for_discord_channel(
        session=session,
        tenant_id=tenant.tenant_id,
        channel_id=normalized_channel_id,
    )
    if project is not None:
        issue_key = get_thread_issue_key(discord_config=project.discord_config, channel_id=normalized_channel_id)
        if issue_key and issue_key_pattern.fullmatch(issue_key) is not None:
            _, cycle = active_case_and_cycle_for_issue(
                session=session,
                tenant_id=tenant.tenant_id,
                issue_key=issue_key,
            )
            if cycle is not None:
                return tenant.tenant_id, issue_key
            project.discord_config = remove_thread_issue_key(
                discord_config=project.discord_config,
                channel_id=normalized_channel_id,
            )
    issue_key = get_thread_issue_key(discord_config=tenant.discord_config, channel_id=normalized_channel_id)
    if not issue_key or issue_key_pattern.fullmatch(issue_key) is None:
        return None
    _, cycle = active_case_and_cycle_for_issue(
        session=session,
        tenant_id=tenant.tenant_id,
        issue_key=issue_key,
    )
    if cycle is None:
        tenant.discord_config = remove_thread_issue_key(
            discord_config=tenant.discord_config,
            channel_id=normalized_channel_id,
        )
        return None
    return tenant.tenant_id, issue_key


def _project_thread_channel_ids_for_tenant(*, session: Session, tenant_id: str, config_key: str) -> set[str]:
    projects = session.execute(
        select(Project).where(
            Project.tenant_id == tenant_id,
            Project.is_archived.is_(False),
        )
    ).scalars().all()
    channel_ids: set[str] = set()
    for project in projects:
        raw_thread_ids = (project.discord_config or {}).get(config_key)
        if not isinstance(raw_thread_ids, list):
            continue
        for value in raw_thread_ids:
            normalized = str(value or "").strip()
            if normalized:
                channel_ids.add(normalized)
    return channel_ids
