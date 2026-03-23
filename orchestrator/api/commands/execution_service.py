from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.communications.command_pipeline import (
    CommandExecutionContext,
    CommandScope,
    dispatch_registered_command,
    resolve_command_ingress_policy,
)


@dataclass(frozen=True)
class CommandExecutionDependencies:
    get_tenant: Callable[[Session, str], Any]
    parse_ingress_source: Callable[[str], Any]
    resolve_discord_command: Callable[[Any, str, str | None, bool], tuple[str, str, list[str]]]
    assert_channel_scope: Callable[[Session, Any, str | None], None]
    assert_sensitive_command_permission: Callable[[Session, Any, str, str, str | None], None]
    resolve_scope: Callable[[Session, Any, str | None], CommandScope]
    enrich_scope: Callable[[Session, Any, str, tuple[str, ...], Any, CommandScope], CommandScope]
    build_handler_registry: Callable[[CommandExecutionContext], dict[str, tuple[Callable, ...]]]


def execute_tenant_command(
    *,
    tenant_id: str,
    payload: DiscordCommandRequest,
    session: Session,
    defer_seed_issues: bool,
    require_ask_confirmation: bool,
    allow_plain_ask: bool,
    ingress_source: str,
    deps: CommandExecutionDependencies,
) -> DiscordCommandResponse:
    tenant = deps.get_tenant(session, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown tenant")
    if not tenant.is_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant is disabled")

    try:
        normalized_ingress_source = deps.parse_ingress_source(ingress_source)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if str(getattr(normalized_ingress_source, "value", normalized_ingress_source)) == "discord":
        deps.assert_channel_scope(session, tenant, payload.channel_id)

    policy = resolve_command_ingress_policy(normalized_ingress_source)
    raw_command = payload.command.strip()
    _, command_name, arguments = deps.resolve_discord_command(
        tenant,
        raw_command,
        payload.channel_id,
        policy.allow_plain_ask,
    )
    deps.assert_sensitive_command_permission(
        session,
        tenant,
        command_name,
        payload.user_id,
        payload.channel_id,
    )

    normalized_user_id = payload.user_id.strip()
    normalized_channel_id = payload.channel_id.strip() if payload.channel_id else "__dm__"
    command_scope = deps.resolve_scope(session, tenant, payload.channel_id)
    command_scope = deps.enrich_scope(
        session,
        tenant,
        command_name,
        tuple(arguments),
        payload,
        command_scope,
    )
    context = CommandExecutionContext(
        command_name=command_name,
        arguments=tuple(arguments),
        tenant_id=tenant_id,
        tenant=tenant,
        session=session,
        payload=payload,
        normalized_user_id=normalized_user_id,
        normalized_channel_id=normalized_channel_id,
        flags={
            "defer_seed_issues": defer_seed_issues,
            "require_ask_confirmation": policy.require_ask_confirmation,
            "allow_plain_ask": policy.allow_plain_ask,
        },
        ingress_source=normalized_ingress_source,
        scope=command_scope,
    )

    handler_registry = deps.build_handler_registry(context)
    try:
        return dispatch_registered_command(context=context, registry=handler_registry)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
