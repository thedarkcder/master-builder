from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class IngressSource(str, Enum):
    DISCORD = "discord"
    JIRA_COMMENT = "jira_comment"


def parse_ingress_source(value: str) -> IngressSource:
    try:
        return IngressSource(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported ingress source: {value}") from exc


@dataclass(frozen=True)
class CommandIngressPolicy:
    require_ask_confirmation: bool
    allow_plain_ask: bool


def resolve_command_ingress_policy(source: IngressSource) -> CommandIngressPolicy:
    normalized = str(getattr(source, "value", source) or "").strip()
    if normalized == IngressSource.DISCORD.value:
        return CommandIngressPolicy(
            require_ask_confirmation=True,
            allow_plain_ask=True,
        )
    if normalized == IngressSource.JIRA_COMMENT.value:
        return CommandIngressPolicy(
            require_ask_confirmation=False,
            allow_plain_ask=False,
        )
    raise ValueError(f"Unsupported ingress source: {source}")


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
    ingress_source: IngressSource = IngressSource.DISCORD
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
