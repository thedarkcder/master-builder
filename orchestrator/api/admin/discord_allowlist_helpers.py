from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.platform.secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
)
from orchestrator.api.schemas import DiscordAllowlistRequestRead


def parse_discord_allowlist_requests(
    discord_config: dict | None,
    *,
    project_id: str | None = None,
) -> list[DiscordAllowlistRequestRead]:
    if not isinstance(discord_config, dict):
        return []
    raw = discord_config.get("allowlist_requests")
    if not isinstance(raw, list):
        return []
    normalized: list[DiscordAllowlistRequestRead] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            continue
        permissions_raw = item.get("permissions")
        permissions = (
            [str(value).strip() for value in permissions_raw if str(value).strip()]
            if isinstance(permissions_raw, list)
            else []
        )
        normalized.append(
            DiscordAllowlistRequestRead(
                project_id=project_id,
                user_id=user_id,
                requested_at=str(item.get("requested_at") or "").strip()
                or datetime.now(timezone.utc).isoformat(),
                channel_id=str(item.get("channel_id") or "").strip() or None,
                reason=str(item.get("reason") or "").strip() or None,
                permissions=permissions,
            )
        )
    normalized.sort(key=lambda item: item.requested_at, reverse=True)
    return normalized


def notify_discord_allowlist_approved(
    *,
    session: Session,
    settings,
    tenant_id: str,
    user_id: str,
    resolve_secret_ref_fn,
    discord_client_factory,
) -> bool:  # noqa: ANN001
    bot_token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not bot_token_ref:
        return False
    bot_token = resolve_secret_ref_fn(
        session,
        secret_ref=bot_token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        return False
    client = discord_client_factory(bot_token=bot_token)
    client.send_direct_message(
        user_id=user_id,
        content="Your allowlist request has been approved. You can now run sensitive commands for this tenant.",
    )
    return True
