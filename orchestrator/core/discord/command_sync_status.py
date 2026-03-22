from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import Lock


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


_LOCK = Lock()
_STATUS = DiscordCommandSyncStatusSnapshot()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def update_discord_command_sync_status(**changes) -> DiscordCommandSyncStatusSnapshot:  # noqa: ANN003
    global _STATUS
    with _LOCK:
        _STATUS = replace(_STATUS, **changes)
        return _STATUS


def mark_discord_command_sync_attempt(
    *,
    bot_token_configured: bool,
    guild_id_configured: bool,
    guild_id: str | None = None,
) -> DiscordCommandSyncStatusSnapshot:
    return update_discord_command_sync_status(
        last_attempt_at=_utc_now(),
        bot_token_configured=bot_token_configured,
        guild_id_configured=guild_id_configured,
        guild_id=(guild_id or "").strip() or None,
        last_failure_reason=None,
        last_error=None,
        healthy=False,
    )


def mark_discord_command_sync_failure(
    *,
    reason: str,
    error: str | None = None,
    bot_token_configured: bool,
    guild_id_configured: bool,
    guild_id: str | None = None,
) -> DiscordCommandSyncStatusSnapshot:
    current = mark_discord_command_sync_attempt(
        bot_token_configured=bot_token_configured,
        guild_id_configured=guild_id_configured,
        guild_id=guild_id,
    )
    return update_discord_command_sync_status(
        synced=current.synced,
        healthy=False,
        last_success_at=current.last_success_at,
        last_failure_reason=reason,
        last_error=(error or "").strip() or None,
    )


def mark_discord_command_sync_success(
    *,
    bot_token_configured: bool,
    guild_id_configured: bool,
    guild_id: str,
    application_id: str,
    command_count: int,
) -> DiscordCommandSyncStatusSnapshot:
    now = _utc_now()
    return update_discord_command_sync_status(
        synced=True,
        healthy=True,
        interaction_ingress_ready=True,
        bot_token_configured=bot_token_configured,
        guild_id_configured=guild_id_configured,
        last_attempt_at=now,
        last_success_at=now,
        last_failure_reason=None,
        last_error=None,
        guild_id=guild_id.strip() or None,
        application_id=application_id.strip() or None,
        command_count=max(0, int(command_count)),
    )


def get_discord_command_sync_status() -> DiscordCommandSyncStatusSnapshot:
    with _LOCK:
        return _STATUS


def reset_discord_command_sync_status() -> None:
    global _STATUS
    with _LOCK:
        _STATUS = DiscordCommandSyncStatusSnapshot()
