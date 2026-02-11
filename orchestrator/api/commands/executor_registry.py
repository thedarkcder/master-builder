from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse

TenantCommandExecutor = Callable[
    [str, DiscordCommandRequest, Session, bool, bool, bool, str],
    DiscordCommandResponse,
]

_tenant_command_executor: TenantCommandExecutor | None = None


def register_tenant_command_executor(executor: TenantCommandExecutor) -> None:
    global _tenant_command_executor
    _tenant_command_executor = executor


def get_tenant_command_executor() -> TenantCommandExecutor | None:
    return _tenant_command_executor


def clear_tenant_command_executor() -> None:
    global _tenant_command_executor
    _tenant_command_executor = None
