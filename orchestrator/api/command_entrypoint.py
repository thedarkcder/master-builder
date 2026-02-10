from __future__ import annotations

import importlib

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.command_executor_registry import get_tenant_command_executor, register_tenant_command_executor
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
    executor = get_tenant_command_executor()
    if executor is None:
        # Ensure the Discord adapter module has had a chance to register itself.
        discord_routes_module = importlib.import_module("orchestrator.api.routes_discord")
        registered_executor = getattr(discord_routes_module, "execute_discord_command", None)
        if callable(registered_executor):
            register_tenant_command_executor(registered_executor)
        executor = get_tenant_command_executor()
    if executor is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Discord command executor is not registered",
        )
    return executor(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source=ingress_source,
    )
