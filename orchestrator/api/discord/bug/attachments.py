from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import uuid4
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from sqlalchemy.orm import Session

from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.storage.models import Tenant
from orchestrator.tools.discord_api import DiscordApiClient
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError

logger = logging.getLogger(__name__)
_SNIPPET_LIMIT = 240
_HTTP_STATUS_PATTERN = re.compile(r"(?:HTTP\s+|\()(?P<status>\d{3})(?:\)|:)")
_DISCORD_ATTACHMENT_USER_AGENT = "MasterBuilder-DiscordAttachmentDownloader/1.0"
_DISCORD_ATTACHMENT_MAX_BYTES = 25_000_000
_DISCORD_ATTACHMENT_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}


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


def _validate_discord_attachment_url(url: str) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _DISCORD_ATTACHMENT_HOSTS
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(
            "Discord attachment downloads require an approved HTTPS CDN URL"
        )


class _DiscordAttachmentRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        _validate_discord_attachment_url(newurl)
        if urlparse(newurl).hostname != urlparse(req.full_url).hostname:
            raise ValueError(
                "Discord attachment redirects must retain the original HTTPS origin"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download_discord_attachment(*, url: str) -> tuple[bytes, str | None]:
    """Fetch a signed Discord CDN URL without credentials, up to 25 MB."""
    _validate_discord_attachment_url(url)
    request = Request(
        url=url,
        headers={"Accept": "*/*", "User-Agent": _DISCORD_ATTACHMENT_USER_AGENT},
        method="GET",
    )
    try:
        with build_opener(_DiscordAttachmentRedirectHandler()).open(
            request, timeout=30
        ) as response:
            payload = response.read(_DISCORD_ATTACHMENT_MAX_BYTES + 1)
            content_type = response.headers.get("Content-Type")
    except HTTPError as exc:
        raise AtlassianOAuthError(
            f"HTTP {exc.code} downloading Discord attachment"
        ) from exc
    except URLError as exc:
        raise AtlassianOAuthError("Failed to download Discord attachment") from exc
    if len(payload) > _DISCORD_ATTACHMENT_MAX_BYTES:
        raise ValueError("Discord attachment exceeds the 25 MB size limit")
    if not payload:
        raise AtlassianOAuthError("Downloaded attachment was empty")
    return payload, content_type.strip() if isinstance(
        content_type, str
    ) and content_type.strip() else None


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

            content_type = (
                str(attachment.get("content_type") or "").strip()
                or downloaded_content_type
            )
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
            except (AtlassianOAuthError, ValueError) as exc:
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
        except (AtlassianOAuthError, ValueError) as exc:
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
