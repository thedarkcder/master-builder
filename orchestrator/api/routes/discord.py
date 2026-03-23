from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.discord.ingress import executor as discord_executor
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse

router = APIRouter(tags=["discord"])


@router.post("/discord/command/{tenant_id}", response_model=DiscordCommandResponse)
def execute_discord_command_route(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session = Depends(get_session),
    *,
    defer_seed_issues: bool = False,
) -> DiscordCommandResponse:
    return discord_executor.execute_discord_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        ingress_source="discord",
    )
