from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.api.routes_discord import execute_discord_command as _execute_discord_command
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse


def execute_tenant_discord_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
    ingress_source: str = "discord",
) -> DiscordCommandResponse:
    return _execute_discord_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source=ingress_source,
    )
