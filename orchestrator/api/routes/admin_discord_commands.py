from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import DiscordCommandSyncStatusRead
from orchestrator.core.config import get_settings
from orchestrator.core.discord.command_sync_status import (
    get_discord_command_sync_status,
)
from orchestrator.core.discord.commands_sync import sync_discord_guild_commands
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/discord/commands/status", response_model=DiscordCommandSyncStatusRead)
def get_discord_command_status(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DiscordCommandSyncStatusRead:
    return DiscordCommandSyncStatusRead.model_validate(
        get_discord_command_sync_status(session=session),
        from_attributes=True,
    )


@router.post("/discord/commands/sync", response_model=DiscordCommandSyncStatusRead)
def sync_discord_commands(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> DiscordCommandSyncStatusRead:
    sync_discord_guild_commands(settings=get_settings(), session=session)
    return DiscordCommandSyncStatusRead.model_validate(
        get_discord_command_sync_status(session=session),
        from_attributes=True,
    )
