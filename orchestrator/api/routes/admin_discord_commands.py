from __future__ import annotations

from fastapi import APIRouter, Depends

from orchestrator.api.schemas import DiscordCommandSyncStatusRead
from orchestrator.core.config import get_settings
from orchestrator.core.discord.command_sync_status import get_discord_command_sync_status
from orchestrator.core.discord.commands_sync import sync_discord_guild_commands
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/discord/commands/status", response_model=DiscordCommandSyncStatusRead)
def get_discord_command_status(_: str = Depends(require_admin)) -> DiscordCommandSyncStatusRead:
    return DiscordCommandSyncStatusRead.model_validate(
        get_discord_command_sync_status(),
        from_attributes=True,
    )


@router.post("/discord/commands/sync", response_model=DiscordCommandSyncStatusRead)
def sync_discord_commands(_: str = Depends(require_admin)) -> DiscordCommandSyncStatusRead:
    sync_discord_guild_commands(settings=get_settings())
    return DiscordCommandSyncStatusRead.model_validate(
        get_discord_command_sync_status(),
        from_attributes=True,
    )
