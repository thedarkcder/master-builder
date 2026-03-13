from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

from fastapi import Depends

from orchestrator.api.commands.execution_service import CommandExecutionDependencies, execute_tenant_command
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.communications.command_pipeline import CommandExecutionContext, parse_ingress_source


@dataclass(frozen=True)
class DiscordIngressHandlers:
    simple: Callable[[CommandExecutionContext], DiscordCommandResponse | None]
    ask: Callable[[CommandExecutionContext], DiscordCommandResponse | None]
    bug_gap: Callable[[CommandExecutionContext], DiscordCommandResponse | None]
    issues: Callable[[CommandExecutionContext], DiscordCommandResponse | None]
    run_control: Callable[[CommandExecutionContext], DiscordCommandResponse | None]


@dataclass(frozen=True)
class DiscordIngressDependencies:
    get_tenant: Callable
    resolve_discord_command: Callable
    assert_channel_scope: Callable
    assert_sensitive_command_permission: Callable
    resolve_scope: Callable
    handlers: DiscordIngressHandlers


def execute_tenant_command_ingress(
    tenant_id: str,
    payload: DiscordCommandRequest,
    session=Depends(get_session),  # noqa: B008
    *,
    defer_seed_issues: bool = False,
    require_ask_confirmation: bool = False,
    allow_plain_ask: bool = False,
    ingress_source: Literal["discord", "jira_comment"] = "discord",
    deps: DiscordIngressDependencies,
) -> DiscordCommandResponse:
    handlers = deps.handlers

    def _build_handler_registry(_: CommandExecutionContext) -> dict[str, tuple]:
        return {
            "help": (handlers.simple,),
            "policy": (handlers.simple,),
            "status": (handlers.simple,),
            "runs": (handlers.simple,),
            "link": (handlers.simple,),
            "request": (handlers.simple,),
            "ask": (handlers.ask,),
            "bug": (handlers.bug_gap,),
            "gap": (handlers.bug_gap,),
            "issues": (handlers.issues,),
            "run": (handlers.run_control,),
            "cancel": (handlers.run_control,),
            "retry": (handlers.run_control,),
            "reply": (handlers.run_control,),
        }

    runtime_deps = CommandExecutionDependencies(
        get_tenant=deps.get_tenant,
        parse_ingress_source=parse_ingress_source,
        resolve_discord_command=deps.resolve_discord_command,
        assert_channel_scope=deps.assert_channel_scope,
        assert_sensitive_command_permission=deps.assert_sensitive_command_permission,
        resolve_scope=deps.resolve_scope,
        build_handler_registry=_build_handler_registry,
    )
    try:
        response = execute_tenant_command(
            tenant_id=tenant_id,
            payload=payload,
            session=session,
            defer_seed_issues=defer_seed_issues,
            require_ask_confirmation=require_ask_confirmation,
            allow_plain_ask=allow_plain_ask,
            ingress_source=ingress_source,
            deps=runtime_deps,
        )
        session.commit()
        return response
    except Exception:
        session.rollback()
        raise
