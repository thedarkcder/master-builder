from __future__ import annotations

import logging
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.storage.models import Tenant
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools.jira_oauth import JiraOAuthError

logger = logging.getLogger(__name__)


def resolve_discord_channel_name(
    *,
    session: Session,
    tenant: Tenant,
    channel_id: str | None,
    discord_bot_token_secret_ref: str,
    secrets_encryption_key: str,
) -> str | None:
    normalized_channel_id = str(channel_id or "").strip()
    if not normalized_channel_id:
        return None
    token_ref = discord_bot_token_secret_ref.strip()
    if not token_ref:
        return None
    bot_token = resolve_scoped_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=secrets_encryption_key,
        tenant_id=tenant.tenant_id,
    )
    if not bot_token:
        return None
    api_client = DiscordApiClient(bot_token=bot_token)
    try:
        response = api_client.get_channel(channel_id=normalized_channel_id)
    except Exception as exc:  # pragma: no cover - network/service failures are non-fatal for display name lookup
        logger.exception(
            "discord_channel_lookup_failed tenant_id=%s channel_id=%s error=%s",
            tenant.tenant_id,
            normalized_channel_id,
            exc,
        )
        return None
    if isinstance(response, dict):
        channel_name = str(response.get("name") or "").strip()
        if channel_name:
            return f"#{channel_name}"
    return None


def download_discord_attachment(*, url: str) -> tuple[bytes, str | None]:
    request = Request(
        url=url,
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise JiraOAuthError(f"HTTP {exc.code} downloading attachment: {body}") from exc
    except URLError as exc:
        raise JiraOAuthError(f"Failed to download attachment: {exc.reason}") from exc

    if not payload:
        raise JiraOAuthError("Downloaded attachment was empty")
    return payload, content_type.strip() if isinstance(content_type, str) and content_type.strip() else None


def upload_discord_attachments_to_jira(
    *,
    client,
    access_token: str,
    cloud_id: str,
    issue_key: str,
    attachments: list[dict[str, str]],
    download_attachment=download_discord_attachment,
) -> tuple[int, list[str]]:
    if not attachments:
        return 0, []

    uploaded_count = 0
    warnings: list[str] = []
    for attachment in attachments:
        filename = str(attachment.get("filename") or "").strip() or "attachment"
        url = str(attachment.get("url") or "").strip()
        if not url:
            warnings.append(f"{filename}: missing URL")
            continue
        try:
            content, downloaded_content_type = download_attachment(url=url)
            content_type = str(attachment.get("content_type") or "").strip() or downloaded_content_type
            client.upload_issue_attachment(
                access_token=access_token,
                cloud_id=cloud_id,
                issue_id_or_key=issue_key,
                filename=filename,
                content=content,
                content_type=content_type,
            )
            uploaded_count += 1
        except (JiraOAuthError, ValueError) as exc:
            warnings.append(f"{filename}: {exc}")
    return uploaded_count, warnings
