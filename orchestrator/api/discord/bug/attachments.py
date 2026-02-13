from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import uuid4
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.storage.models import Tenant
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools.jira_oauth import JiraOAuthError

logger = logging.getLogger(__name__)
_SNIPPET_LIMIT = 240
_HTTP_STATUS_PATTERN = re.compile(r"(?:HTTP\s+|\()(?P<status>\d{3})(?:\)|:)")


@dataclass(frozen=True)
class AttachmentUploadFailure:
    filename: str
    source: str
    status: int | None
    detail: str
    response_snippet: str | None
    correlation_id: str

    def warning_message(self) -> str:
        status_text = f" (HTTP {self.status})" if self.status else ""
        snippet = self.response_snippet or self.detail
        if self.source == "jira_upload":
            source_label = "Jira upload"
        elif self.source == "discord_fetch":
            source_label = "Discord fetch"
        else:
            source_label = self.source
        return f"{self.filename}{status_text}: {source_label} failure: {snippet}"

    def as_dict(self) -> dict[str, str | int | None]:
        return {
            "filename": self.filename,
            "source": self.source,
            "status": self.status,
            "detail": self.detail,
            "response_snippet": self.response_snippet,
            "correlation_id": self.correlation_id,
        }


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
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=secrets_encryption_key,
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


def _is_discord_attachment_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host.endswith("discordapp.com") or host.endswith("discord.com")


def download_discord_attachment(*, url: str, bot_token: str | None = None) -> tuple[bytes, str | None]:
    headers: dict[str, str] = {}
    normalized_token = str(bot_token or "").strip()
    if normalized_token and _is_discord_attachment_url(url):
        headers["Authorization"] = f"Bot {normalized_token}"
    request = Request(url=url, headers=headers, method="GET")
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


def _extract_status_and_snippet(
    message: str | None,
) -> tuple[int | None, str | None]:
    text = str(message or "").strip()
    if not text:
        return None, None
    match = _HTTP_STATUS_PATTERN.search(text)
    status = int(match.group("status")) if match else None
    if ":" in text:
        snippet = text.split(":", 1)[1].strip()
    else:
        snippet = text
    if len(snippet) > _SNIPPET_LIMIT:
        snippet = f"{snippet[: _SNIPPET_LIMIT - 3]}..."
    return status, snippet or None


def upload_discord_attachments_to_jira(
    *,
    client,
    access_token: str,
    cloud_id: str,
    issue_key: str,
    attachments: list[dict[str, str]],
    correlation_id: str | None = None,
    download_attachment=download_discord_attachment,
) -> tuple[int, list[AttachmentUploadFailure]]:
    if not attachments:
        return 0, []

    uploaded_count = 0
    failures: list[AttachmentUploadFailure] = []
    upload_correlation_id = str(correlation_id or uuid4().hex)
    for attachment in attachments:
        filename = str(attachment.get("filename") or "").strip() or "attachment"
        url = str(attachment.get("url") or "").strip()
        if not url:
            failures.append(
                AttachmentUploadFailure(
                    filename=filename,
                    source="discord_fetch",
                    status=None,
                    detail="missing URL",
                    response_snippet="missing URL",
                    correlation_id=upload_correlation_id,
                )
            )
            logger.error(
                "discord_attachment_upload_failed issue_id_or_key=%s source=%s filename=%s status=%s correlation_id=%s detail=%s",
                issue_key,
                "discord_fetch",
                filename,
                None,
                upload_correlation_id,
                "missing URL",
            )
            continue
        try:
            content, downloaded_content_type = download_attachment(url=url)
            content_type = str(attachment.get("content_type") or "").strip() or downloaded_content_type
            try:
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
                error_text = str(exc)
                status, snippet = _extract_status_and_snippet(error_text)
                failures.append(
                    AttachmentUploadFailure(
                        filename=filename,
                        source="jira_upload",
                        status=status,
                        detail=error_text,
                        response_snippet=snippet,
                        correlation_id=upload_correlation_id,
                    )
                )
                logger.error(
                    "discord_attachment_upload_failed issue_id_or_key=%s source=%s filename=%s status=%s correlation_id=%s detail=%s response_snippet=%s",
                    issue_key,
                    "jira_upload",
                    filename,
                    status,
                    upload_correlation_id,
                    error_text,
                    snippet,
                )
        except (JiraOAuthError, ValueError) as exc:
            error_text = str(exc)
            status, snippet = _extract_status_and_snippet(error_text)
            failures.append(
                AttachmentUploadFailure(
                    filename=filename,
                    source="discord_fetch",
                    status=status,
                    detail=error_text,
                    response_snippet=snippet,
                    correlation_id=upload_correlation_id,
                )
            )
            logger.error(
                "discord_attachment_upload_failed issue_id_or_key=%s source=%s filename=%s status=%s correlation_id=%s detail=%s response_snippet=%s",
                issue_key,
                "discord_fetch",
                filename,
                status,
                upload_correlation_id,
                error_text,
                snippet,
            )
    return uploaded_count, failures
