from __future__ import annotations

from collections.abc import Callable

from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.storage.models import Tenant


def rewrite_plain_text_command(
    *,
    session,
    tenant: Tenant,
    payload: DiscordCommandRequest,
    allow_plain_ask: bool,
    find_seed_followup_context_fn: Callable[..., object | None],
) -> str:  # noqa: ANN001
    raw_command = str(payload.command or "")
    normalized_command = raw_command.strip()
    if not allow_plain_ask or not normalized_command or normalized_command.startswith("!"):
        return raw_command

    normalized_channel_id = str(payload.channel_id or "").strip()
    normalized_user_id = str(payload.user_id or "").strip()
    if normalized_channel_id:
        seed_followup = find_seed_followup_context_fn(
            session=session,
            tenant=tenant,
            channel_id=normalized_channel_id,
            user_id=normalized_user_id or None,
        )
        if seed_followup is not None:
            return f"!issues followup {normalized_command}"

    return raw_command
