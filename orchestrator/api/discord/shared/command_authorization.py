from __future__ import annotations

from collections.abc import Callable

from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.storage.models import Tenant


def allow_sensitive_command_bypass(
    *,
    session,
    tenant: Tenant,
    command_name: str,
    arguments: tuple[str, ...],
    payload: DiscordCommandRequest,
    find_seed_followup_context_fn: Callable[..., object | None],
) -> bool:  # noqa: ANN001
    if command_name != "issues":
        return False
    if not arguments or str(arguments[0] or "").strip().lower() != "followup":
        return False

    normalized_channel_id = str(payload.channel_id or "").strip()
    normalized_user_id = str(payload.user_id or "").strip()
    if not normalized_channel_id or not normalized_user_id:
        return False

    context = find_seed_followup_context_fn(
        session=session,
        tenant=tenant,
        channel_id=normalized_channel_id,
        user_id=normalized_user_id,
    )
    return context is not None
