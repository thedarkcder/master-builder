from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings, get_settings
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import DiscordCommandSyncRuntimeState

RUNTIME_NAME = "discord-command-sync"


@dataclass(frozen=True)
class DiscordCommandSyncStatusSnapshot:
    synced: bool = False
    healthy: bool = False
    interaction_ingress_ready: bool = True
    bot_token_configured: bool = False
    guild_id_configured: bool = False
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_failure_reason: str | None = None
    last_error: str | None = None
    guild_id: str | None = None
    application_id: str | None = None
    command_count: int = 0


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def discord_command_sync_service_instance_id() -> str:
    return f"{os.uname().nodename}:{os.getpid()}"


def _row_to_snapshot(
    row: DiscordCommandSyncRuntimeState | None,
) -> DiscordCommandSyncStatusSnapshot:
    if row is None:
        return DiscordCommandSyncStatusSnapshot()
    return DiscordCommandSyncStatusSnapshot(
        synced=bool(row.synced),
        healthy=bool(row.healthy),
        interaction_ingress_ready=bool(row.interaction_ingress_ready),
        bot_token_configured=bool(row.bot_token_configured),
        guild_id_configured=bool(row.guild_id_configured),
        last_attempt_at=row.last_attempt_at,
        last_success_at=row.last_success_at,
        last_failure_reason=row.last_failure_reason,
        last_error=row.last_error,
        guild_id=row.guild_id,
        application_id=row.application_id,
        command_count=max(0, int(row.command_count or 0)),
    )


def _get_or_create_row(
    *,
    session: Session,
    service_instance_id: str | None = None,
) -> DiscordCommandSyncRuntimeState:
    row = session.get(DiscordCommandSyncRuntimeState, RUNTIME_NAME)
    if row is None:
        row = DiscordCommandSyncRuntimeState(
            runtime_name=RUNTIME_NAME,
            synced=False,
            healthy=False,
            interaction_ingress_ready=True,
            bot_token_configured=False,
            guild_id_configured=False,
            command_count=0,
            service_instance_id=service_instance_id,
            updated_at=_utc_now(),
        )
        session.add(row)
        return row
    if service_instance_id is not None:
        row.service_instance_id = service_instance_id
    return row


def get_discord_command_sync_status(
    *,
    session: Session | None = None,
    settings: Settings | None = None,
) -> DiscordCommandSyncStatusSnapshot:
    if session is not None:
        return _row_to_snapshot(
            session.get(DiscordCommandSyncRuntimeState, RUNTIME_NAME)
        )

    resolved_settings = settings or get_settings()
    session_factory = create_session_factory(resolved_settings.database_url)
    with session_factory() as owned_session:
        return _row_to_snapshot(
            owned_session.get(DiscordCommandSyncRuntimeState, RUNTIME_NAME)
        )


def mark_discord_command_sync_attempt(
    *,
    session: Session,
    bot_token_configured: bool,
    guild_id_configured: bool,
    guild_id: str | None = None,
    service_instance_id: str | None = None,
) -> DiscordCommandSyncStatusSnapshot:
    row = _get_or_create_row(session=session, service_instance_id=service_instance_id)
    row.last_attempt_at = _utc_now()
    row.bot_token_configured = bool(bot_token_configured)
    row.guild_id_configured = bool(guild_id_configured)
    row.guild_id = (guild_id or "").strip() or None
    row.last_failure_reason = None
    row.last_error = None
    row.healthy = False
    row.interaction_ingress_ready = True
    row.updated_at = _utc_now()
    return _row_to_snapshot(row)


def mark_discord_command_sync_failure(
    *,
    session: Session,
    reason: str,
    error: str | None = None,
    bot_token_configured: bool,
    guild_id_configured: bool,
    guild_id: str | None = None,
    service_instance_id: str | None = None,
) -> DiscordCommandSyncStatusSnapshot:
    row = _get_or_create_row(session=session, service_instance_id=service_instance_id)
    current = _row_to_snapshot(row)
    row.last_attempt_at = _utc_now()
    row.bot_token_configured = bool(bot_token_configured)
    row.guild_id_configured = bool(guild_id_configured)
    row.guild_id = (guild_id or "").strip() or None
    row.synced = current.synced
    row.healthy = False
    row.last_success_at = current.last_success_at
    row.last_failure_reason = str(reason or "").strip() or None
    row.last_error = (error or "").strip() or None
    row.interaction_ingress_ready = True
    row.updated_at = _utc_now()
    return _row_to_snapshot(row)


def mark_discord_command_sync_success(
    *,
    session: Session,
    bot_token_configured: bool,
    guild_id_configured: bool,
    guild_id: str,
    application_id: str,
    command_count: int,
    service_instance_id: str | None = None,
) -> DiscordCommandSyncStatusSnapshot:
    row = _get_or_create_row(session=session, service_instance_id=service_instance_id)
    now = _utc_now()
    row.synced = True
    row.healthy = True
    row.interaction_ingress_ready = True
    row.bot_token_configured = bool(bot_token_configured)
    row.guild_id_configured = bool(guild_id_configured)
    row.last_attempt_at = now
    row.last_success_at = now
    row.last_failure_reason = None
    row.last_error = None
    row.guild_id = guild_id.strip() or None
    row.application_id = application_id.strip() or None
    row.command_count = max(0, int(command_count))
    row.updated_at = now
    return _row_to_snapshot(row)


def reset_discord_command_sync_status(
    *,
    session: Session | None = None,
    settings: Settings | None = None,
) -> None:
    if session is not None:
        row = session.get(DiscordCommandSyncRuntimeState, RUNTIME_NAME)
        if row is not None:
            session.delete(row)
        return

    resolved_settings = settings or get_settings()
    session_factory = create_session_factory(resolved_settings.database_url)
    with session_factory() as owned_session:
        row = owned_session.get(DiscordCommandSyncRuntimeState, RUNTIME_NAME)
        if row is not None:
            owned_session.delete(row)
            owned_session.commit()
