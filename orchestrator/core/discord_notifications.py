from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.secret_manager import resolve_secret_ref
from orchestrator.storage.models import Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordSendResult:
    sent: bool
    reason: str
    channel_id: str | None = None


def send_tenant_discord_message(
    *,
    session: Session,
    tenant: Tenant,
    message: str,
    settings: Settings,
) -> DiscordSendResult:
    if not message.strip():
        return DiscordSendResult(sent=False, reason="empty_message")

    discord_config = tenant.discord_config or {}
    channel_id = str(discord_config.get("channel_id") or "").strip()
    if not channel_id:
        return DiscordSendResult(sent=False, reason="channel_not_configured")

    token_ref = settings.discord_bot_token_secret_ref.strip()
    if not token_ref:
        return DiscordSendResult(sent=False, reason="bot_token_ref_not_configured")

    bot_token = resolve_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        return DiscordSendResult(sent=False, reason=f"bot_token_missing:{token_ref}", channel_id=channel_id)

    try:
        client = DiscordApiClient(bot_token=bot_token)
        client.post_message(channel_id=channel_id, content=message)
    except (DiscordApiError, ValueError) as exc:
        logger.warning(
            "discord_message_send_failed tenant_id=%s channel_id=%s error=%s",
            tenant.tenant_id,
            channel_id,
            exc,
        )
        return DiscordSendResult(sent=False, reason=f"send_failed:{exc}", channel_id=channel_id)

    return DiscordSendResult(sent=True, reason="sent", channel_id=channel_id)
