from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.request import Request as UrlRequest, urlopen

from sqlalchemy.orm import Session

from orchestrator.api.discord.shared.errors import DiscordInteractionWebhookExpiredError
from orchestrator.core.platform.secret_service import PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
from orchestrator.tools.discord_api import DiscordApiClient


def discord_api_client(*, session: Session, settings, resolve_platform_secret_ref_fn) -> DiscordApiClient:  # noqa: ANN001
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        raise RuntimeError("Discord bot token secret ref is not configured")
    bot_token = resolve_platform_secret_ref_fn(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise RuntimeError(f"Discord bot token secret '{token_ref}' is missing")
    return DiscordApiClient(bot_token=bot_token)



def send_discord_interaction_followup(
    *,
    application_id: str,
    interaction_token: str,
    content: str,
    ephemeral: bool = False,
    components: list[dict] | None = None,
    reply_to_message_id: str | None = None,
    channel_id: str | None = None,
) -> None:
    normalized_app_id = application_id.strip()
    normalized_token = interaction_token.strip()
    normalized_content = content.strip()
    if not normalized_app_id or not normalized_token or not normalized_content:
        raise ValueError("Discord interaction follow-up payload is incomplete")
    payload: dict[str, object] = {"content": normalized_content}
    if ephemeral:
        payload["flags"] = 64
    if components:
        payload["components"] = components
    if reply_to_message_id and channel_id:
        payload["message_reference"] = {
            "message_id": reply_to_message_id,
            "channel_id": channel_id,
            "fail_if_not_exists": False,
        }
    request = UrlRequest(
        url=f"https://discord.com/api/v10/webhooks/{normalized_app_id}/{interaction_token}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MasterBuilderDiscordClient/1.0 (+https://github.com/thedarkcder/master-builder)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30):
            return
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        normalized_body = error_body.casefold()
        if exc.code == 404 and ("unknown webhook" in normalized_body or '"code": 10015' in normalized_body):
            raise DiscordInteractionWebhookExpiredError(
                f"Discord interaction follow-up webhook expired ({exc.code}): {error_body}"
            ) from exc
        raise RuntimeError(f"Discord follow-up request failed ({exc.code}): {error_body}") from exc


def send_discord_interaction_callback(
    *,
    interaction_id: str,
    interaction_token: str,
    response_body: bytes,
) -> None:
    normalized_interaction_id = interaction_id.strip()
    normalized_token = interaction_token.strip()
    normalized_body = response_body.strip()
    if not normalized_interaction_id or not normalized_token or not normalized_body:
        raise ValueError("Discord interaction callback payload is incomplete")
    request = UrlRequest(
        url=f"https://discord.com/api/v10/interactions/{normalized_interaction_id}/{normalized_token}/callback",
        data=normalized_body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "MasterBuilderDiscordClient/1.0 (+https://github.com/thedarkcder/master-builder)",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            return
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8")
        raise RuntimeError(f"Discord interaction callback failed ({exc.code}): {error_body}") from exc
