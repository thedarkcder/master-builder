from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

from orchestrator.api.discord.bug.attachments import (
    AttachmentUploadFailure,
    download_discord_attachment,
    resolve_discord_channel_name,
    upload_discord_attachments_to_jira,
)
from orchestrator.tools.jira_oauth import JiraOAuthError


class _Response:
    def __init__(self, payload: bytes, content_type: str | None = None) -> None:
        self._payload = payload
        self.headers = {"Content-Type": content_type} if content_type is not None else {}

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class DiscordBugAttachmentsTests(unittest.TestCase):
    def test_resolve_discord_channel_name_guards_and_success(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1")

        self.assertIsNone(
            resolve_discord_channel_name(
                session=session,
                tenant=tenant,
                channel_id="",
                discord_bot_token_secret_ref="token/ref",
                secrets_encryption_key="enc",
            )
        )

        with patch("orchestrator.api.discord.bug.attachments.resolve_platform_secret_ref", return_value=""):
            self.assertIsNone(
                resolve_discord_channel_name(
                    session=session,
                    tenant=tenant,
                    channel_id="123",
                    discord_bot_token_secret_ref="token/ref",
                    secrets_encryption_key="enc",
                )
            )

        with (
            patch("orchestrator.api.discord.bug.attachments.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.bug.attachments.DiscordApiClient") as client_cls,
        ):
            client = client_cls.return_value
            client.get_channel.return_value = {"name": "alerts"}
            self.assertEqual(
                resolve_discord_channel_name(
                    session=session,
                    tenant=tenant,
                    channel_id="123",
                    discord_bot_token_secret_ref="token/ref",
                    secrets_encryption_key="enc",
                ),
                "#alerts",
            )

    def test_resolve_discord_channel_name_handles_api_errors(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1")

        with (
            patch("orchestrator.api.discord.bug.attachments.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.bug.attachments.DiscordApiClient") as client_cls,
        ):
            client_cls.return_value.get_channel.side_effect = RuntimeError("boom")
            self.assertIsNone(
                resolve_discord_channel_name(
                    session=session,
                    tenant=tenant,
                    channel_id="123",
                    discord_bot_token_secret_ref="token/ref",
                    secrets_encryption_key="enc",
                )
            )

    def test_download_discord_attachment_success_and_errors(self) -> None:
        with patch("orchestrator.api.discord.bug.attachments.urlopen", return_value=_Response(b"data", "text/plain")):
            payload, content_type = download_discord_attachment(url="https://discord.test/file")
        self.assertEqual(payload, b"data")
        self.assertEqual(content_type, "text/plain")

        http_error = HTTPError(
            url="https://discord.test/file",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"denied"),
        )
        with patch("orchestrator.api.discord.bug.attachments.urlopen", side_effect=http_error):
            with self.assertRaisesRegex(JiraOAuthError, "HTTP 403"):
                download_discord_attachment(url="https://discord.test/file")

        with patch("orchestrator.api.discord.bug.attachments.urlopen", side_effect=URLError("down")):
            with self.assertRaisesRegex(JiraOAuthError, "Failed to download attachment"):
                download_discord_attachment(url="https://discord.test/file")

        with patch("orchestrator.api.discord.bug.attachments.urlopen", return_value=_Response(b"", "image/png")):
            with self.assertRaisesRegex(JiraOAuthError, "empty"):
                download_discord_attachment(url="https://discord.test/file")

    def test_download_discord_attachment_uses_bot_auth_for_discord_urls(self) -> None:
        seen_headers: dict[str, str] = {}

        def _fake_urlopen(request, timeout: int = 30):  # noqa: ANN001
            del timeout
            seen_headers.update(dict(request.header_items()))
            return _Response(b"data", "image/png")

        with patch("orchestrator.api.discord.bug.attachments.urlopen", side_effect=_fake_urlopen):
            payload, content_type = download_discord_attachment(
                url="https://cdn.discordapp.com/attachments/1/2/image.png",
                bot_token="test-token",
            )

        self.assertEqual(payload, b"data")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(seen_headers.get("Authorization"), "Bot test-token")

    def test_upload_discord_attachments_to_jira(self) -> None:
        client = MagicMock()
        uploaded_count, warnings = upload_discord_attachments_to_jira(
            client=client,
            access_token="token",
            cloud_id="cloud",
            issue_key="MAB-1",
            attachments=[],
        )
        self.assertEqual(uploaded_count, 0)
        self.assertEqual(warnings, [])

        def _download(*, url: str) -> tuple[bytes, str | None]:
            if url.endswith("bad"):
                raise JiraOAuthError("failed")
            if url.endswith("value"):
                raise ValueError("bad-value")
            return b"payload", "application/octet-stream"

        attachments = [
            {"filename": "missing-url"},
            {"filename": "ok", "url": "https://discord.test/ok", "content_type": "image/png"},
            {"filename": "fail", "url": "https://discord.test/bad"},
            {"filename": "fail-value", "url": "https://discord.test/value"},
        ]
        uploaded_count, warnings = upload_discord_attachments_to_jira(
            client=client,
            access_token="token",
            cloud_id="cloud",
            issue_key="MAB-1",
            attachments=attachments,
            download_attachment=_download,
        )

        self.assertEqual(uploaded_count, 1)
        self.assertEqual(client.upload_issue_attachment.call_count, 1)
        kwargs = client.upload_issue_attachment.call_args.kwargs
        self.assertEqual(kwargs["filename"], "ok")
        self.assertEqual(kwargs["content_type"], "image/png")
        self.assertEqual(len(warnings), 3)
        self.assertIsInstance(warnings[0], AttachmentUploadFailure)
        self.assertEqual(warnings[0].filename, "missing-url")
        self.assertEqual(warnings[0].source, "discord_fetch")
        self.assertEqual(warnings[1].filename, "fail")
        self.assertEqual(warnings[1].source, "discord_fetch")
        self.assertEqual(warnings[2].filename, "fail-value")
        self.assertEqual(warnings[2].source, "discord_fetch")
        self.assertTrue(any(warning.detail == "failed" for warning in warnings))
        self.assertTrue(any(warning.detail == "bad-value" for warning in warnings))

    def test_upload_discord_attachments_to_jira_records_jira_upload_error_metadata(self) -> None:
        client = MagicMock()

        def _download(*, url: str) -> tuple[bytes, str | None]:
            del url
            return b"payload", "application/octet-stream"

        def _upload_issue_attachment(**_: object) -> None:
            raise JiraOAuthError("Jira attachment upload failed (403): permission denied")

        client.upload_issue_attachment = _upload_issue_attachment  # type: ignore[attr-defined]

        uploaded_count, warnings = upload_discord_attachments_to_jira(
            client=client,
            access_token="token",
            cloud_id="cloud",
            issue_key="MAB-1",
            attachments=[{"filename": "fail-upload.png", "url": "https://discord.test/ok"}],
            correlation_id="corr-1",
            download_attachment=_download,
        )

        self.assertEqual(uploaded_count, 0)
        self.assertEqual(len(warnings), 1)
        failure = warnings[0]
        self.assertIsInstance(failure, AttachmentUploadFailure)
        self.assertEqual(failure.source, "jira_upload")
        self.assertEqual(failure.status, 403)
        self.assertEqual(failure.correlation_id, "corr-1")
        self.assertIn("permission denied", failure.response_snippet or "")


if __name__ == "__main__":
    unittest.main()
