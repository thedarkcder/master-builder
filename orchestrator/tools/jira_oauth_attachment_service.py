from __future__ import annotations

from typing import Any
from urllib.parse import quote

from orchestrator.tools.jira_oauth_models import JiraOAuthError


class JiraOAuthAttachmentService:
    def __init__(self, *, post_multipart) -> None:
        self._post_multipart = post_multipart

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
            raise JiraOAuthError("Missing issue id/key for attachment upload")
        if not normalized_filename:
            raise JiraOAuthError("Missing attachment filename")
        if not content:
            raise JiraOAuthError("Attachment payload is empty")

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
            raise JiraOAuthError("Jira attachment upload response was not a list")
        return [item for item in parsed if isinstance(item, dict)]
