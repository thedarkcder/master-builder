from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CommandScope:
    project_id: str | None = None
    project_keys: tuple[str, ...] = ()
    channel_id: str | None = None


@dataclass(frozen=True)
class CommandExecutionContext:
    command_name: str
    arguments: tuple[str, ...]
    tenant_id: str
    tenant: Any
    session: Any
    payload: Any
    normalized_user_id: str
    normalized_channel_id: str
    flags: dict[str, bool]
    ingress_source: str = "discord"
    scope: CommandScope = field(default_factory=CommandScope)


class CommandHandler(Protocol):
    def __call__(self, context: CommandExecutionContext) -> Any | None:
        ...


def dispatch_registered_command(
    *,
    context: CommandExecutionContext,
    registry: dict[str, tuple[CommandHandler, ...]],
) -> Any:
    handlers = registry.get(context.command_name, ())
    if not handlers:
        raise ValueError(f"Unsupported command: {context.command_name}")

    for handler in handlers:
        response = handler(context)
        if response is not None:
            return response

    raise ValueError(f"No handler produced a response for command: {context.command_name}")
