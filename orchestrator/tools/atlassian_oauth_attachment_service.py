from __future__ import annotations

from typing import Any
from urllib.parse import quote

from orchestrator.tools.atlassian_oauth_models import AtlassianOAuthError


class AtlassianOAuthAttachmentService:
    def __init__(self, *, post_multipart, get_bytes) -> None:
        self._post_multipart = post_multipart
        self._get_bytes = get_bytes

    def upload_issue_attachment(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized_issue = issue_id_or_key.strip()
        normalized_filename = filename.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for attachment upload")
        if not normalized_filename:
            raise AtlassianOAuthError("Missing attachment filename")
        if not content:
            raise AtlassianOAuthError("Attachment payload is empty")

        parsed = self._post_multipart(
            url=(
                f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/"
                f"{quote(normalized_issue, safe='')}/attachments"
            ),
            access_token=access_token,
            filename=normalized_filename,
            content=content,
            content_type=(content_type or "application/octet-stream"),
        )
        if not isinstance(parsed, list):
            raise AtlassianOAuthError("Jira attachment upload response was not a list")
        return [item for item in parsed if isinstance(item, dict)]

    def download_attachment(
        self,
        *,
        access_token: str,
        content_url: str,
    ) -> bytes:
        normalized_url = str(content_url or "").strip()
        if not normalized_url:
            raise AtlassianOAuthError("Missing attachment content url")
        content = self._get_bytes(url=normalized_url, access_token=access_token)
        if not isinstance(content, (bytes, bytearray)):
            raise AtlassianOAuthError("Jira attachment download response was not bytes")
        return bytes(content)
