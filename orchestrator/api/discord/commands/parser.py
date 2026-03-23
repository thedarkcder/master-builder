from __future__ import annotations

from orchestrator.api.discord.shared.state import parse_command_text as _parse_command_text
from orchestrator.storage.models import Tenant


def resolve_discord_command(
    *,
    tenant: Tenant,
    raw_command: str,
    channel_id: str | None,
    allow_plain_ask: bool,
) -> tuple[str, str, list[str]]:
    command_text = raw_command
    if allow_plain_ask and raw_command and not raw_command.startswith("!") and command_text == raw_command:
        command_text = f"!ask {raw_command}"
    command_name, arguments = _parse_command_text(command_text)
    return command_text, command_name, arguments
