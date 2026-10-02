from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.commands.executor_registry import get_tenant_command_executor
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse


def _resolve_registered_executor():
    executor = get_tenant_command_executor()
    if executor is not None:
        return executor
    try:
        from orchestrator.api.discord.ingress.executor import (
            register_discord_command_executor,
        )
    except Exception:  # noqa: BLE001
        register_discord_command_executor = None
    if register_discord_command_executor is not None:
        try:
            register_discord_command_executor()
        except Exception:  # noqa: BLE001
            pass
        executor = get_tenant_command_executor()
    return executor


def _execute_registered_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    ingress_source: str = "discord",
) -> DiscordCommandResponse:
    executor = _resolve_registered_executor()
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
        ingress_source=ingress_source,
    )


def execute_tenant_discord_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    ingress_source: str = "discord",
) -> DiscordCommandResponse:
    return _execute_registered_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        ingress_source=ingress_source,
    )


def execute_tenant_discord_ingress_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    ingress_source: str = "discord",
) -> DiscordCommandResponse:
    if ingress_source != "discord":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Discord ingress command wrapper only supports ingress_source='discord'",
        )
    return _execute_registered_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        ingress_source="discord",
    )


def execute_tenant_jira_comment_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    ingress_source: str = "jira_comment",
) -> DiscordCommandResponse:
    if ingress_source != "jira_comment":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Jira comment command wrapper only supports ingress_source='jira_comment'",
        )
    return _execute_registered_command(
        tenant_id=tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=defer_seed_issues,
        require_ask_confirmation=require_ask_confirmation,
        ingress_source="jira_comment",
    )
