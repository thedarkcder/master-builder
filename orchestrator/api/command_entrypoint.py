from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.command_executor_registry import get_tenant_command_executor
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse


def _execute_registered_command(
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
    return _execute_registered_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source=ingress_source,
    )


def execute_tenant_discord_ingress_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
) -> DiscordCommandResponse:
    return _execute_registered_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source="discord",
    )


def execute_tenant_jira_comment_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
) -> DiscordCommandResponse:
    return _execute_registered_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        allow_plain_ask=allow_plain_ask,
        ingress_source="jira_comment",
    )
