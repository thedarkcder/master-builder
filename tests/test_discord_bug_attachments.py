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
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError


class _Response:
    def __init__(self, payload: bytes, content_type: str | None = None) -> None:
        self._payload = payload
        self.headers = (
            {"Content-Type": content_type} if content_type is not None else {}
        )

    def read(self, size: int = -1) -> bytes:
        return self._payload[:size] if size >= 0 else self._payload

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


def _patch_attachment_open(*, return_value=None, side_effect=None):
    opener = SimpleNamespace(
        open=MagicMock(return_value=return_value, side_effect=side_effect)
    )
    return patch(
        "orchestrator.api.discord.bug.attachments.build_opener", return_value=opener
    )


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

        with patch(
            "orchestrator.api.discord.bug.attachments.resolve_platform_secret_ref",
            return_value="",
        ):
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
            patch(
                "orchestrator.api.discord.bug.attachments.resolve_platform_secret_ref",
                return_value="token",
            ),
            patch(
                "orchestrator.api.discord.bug.attachments.DiscordApiClient"
            ) as client_cls,
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
            patch(
                "orchestrator.api.discord.bug.attachments.resolve_platform_secret_ref",
                return_value="token",
            ),
            patch(
                "orchestrator.api.discord.bug.attachments.DiscordApiClient"
            ) as client_cls,
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
        with _patch_attachment_open(return_value=_Response(b"data", "text/plain")):
            payload, content_type = download_discord_attachment(
                url="https://cdn.discordapp.com/attachments/file"
            )
        self.assertEqual(payload, b"data")
        self.assertEqual(content_type, "text/plain")

        http_error = HTTPError(
            url="https://cdn.discordapp.com/attachments/file",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"denied"),
        )
        with _patch_attachment_open(side_effect=http_error):
            with self.assertRaisesRegex(AtlassianOAuthError, "HTTP 403"):
                download_discord_attachment(
                    url="https://cdn.discordapp.com/attachments/file"
                )

        with _patch_attachment_open(side_effect=URLError("down")):
            with self.assertRaisesRegex(
                AtlassianOAuthError, "Failed to download Discord attachment"
            ):
                download_discord_attachment(
                    url="https://cdn.discordapp.com/attachments/file"
                )

        with _patch_attachment_open(return_value=_Response(b"", "image/png")):
            with self.assertRaisesRegex(AtlassianOAuthError, "empty"):
                download_discord_attachment(
                    url="https://cdn.discordapp.com/attachments/file"
                )

    def test_download_discord_attachment_never_uses_bot_auth(self) -> None:
        seen_headers: dict[str, str] = {}
        seen_urls: list[str] = []

        def _fake_urlopen(request, timeout: int = 30):  # noqa: ANN001
            del timeout
            seen_headers.update(dict(request.header_items()))
            seen_urls.append(request.full_url)
            return _Response(b"data", "image/png")

        with _patch_attachment_open(side_effect=_fake_urlopen):
            payload, content_type = download_discord_attachment(
                url="https://cdn.discordapp.com/attachments/1/2/image.png",
            )

        self.assertEqual(payload, b"data")
        self.assertEqual(content_type, "image/png")
        self.assertNotIn("Authorization", seen_headers)
        user_agent = (
            seen_headers.get("User-Agent")
            or seen_headers.get("User-agent")
            or seen_headers.get("user-agent")
        )
        self.assertEqual(user_agent, "MasterBuilder-DiscordAttachmentDownloader/1.0")
        self.assertEqual(
            seen_urls[0], "https://cdn.discordapp.com/attachments/1/2/image.png"
        )

    def test_download_discord_attachment_does_not_retry_alternate_media_host(
        self,
    ) -> None:
        error = HTTPError(
            url="https://cdn.discordapp.com/attachments/1/2/image.png",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"error code: 1010"),
        )
        calls = []

        def open_attachment(request, timeout=30):
            calls.append(request.full_url)
            raise error

        with _patch_attachment_open(side_effect=open_attachment):
            with self.assertRaisesRegex(AtlassianOAuthError, "HTTP 403"):
                download_discord_attachment(
                    url="https://cdn.discordapp.com/attachments/1/2/image.png"
                )
        self.assertEqual(
            calls, ["https://cdn.discordapp.com/attachments/1/2/image.png"]
        )

    def test_download_discord_attachment_does_not_retry_alternate_cdn_host(
        self,
    ) -> None:
        error = HTTPError(
            url="https://media.discordapp.net/attachments/1/2/image.png?width=500&height=500",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"error code: 1010"),
        )
        calls = []

        def open_attachment(request, timeout=30):
            calls.append(request.full_url)
            raise error

        with _patch_attachment_open(side_effect=open_attachment):
            with self.assertRaisesRegex(AtlassianOAuthError, "HTTP 403"):
                download_discord_attachment(
                    url="https://media.discordapp.net/attachments/1/2/image.png?width=500&height=500"
                )
        self.assertEqual(
            calls,
            [
                "https://media.discordapp.net/attachments/1/2/image.png?width=500&height=500"
            ],
        )

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
                raise AtlassianOAuthError("failed")
            if url.endswith("value"):
                raise ValueError("bad-value")
            return b"payload", "application/octet-stream"

        attachments = [
            {"filename": "missing-url"},
            {
                "filename": "ok",
                "url": "https://discord.test/ok",
                "content_type": "image/png",
            },
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

    def test_upload_discord_attachments_to_jira_records_jira_upload_error_metadata(
        self,
    ) -> None:
        client = MagicMock()

        def _download(*, url: str) -> tuple[bytes, str | None]:
            del url
            return b"payload", "application/octet-stream"

        def _upload_issue_attachment(**_: object) -> None:
            raise AtlassianOAuthError(
                "Jira attachment upload failed (403): permission denied"
            )

        client.upload_issue_attachment = _upload_issue_attachment  # type: ignore[attr-defined]

        uploaded_count, warnings = upload_discord_attachments_to_jira(
            client=client,
            access_token="token",
            cloud_id="cloud",
            issue_key="MAB-1",
            attachments=[
                {"filename": "fail-upload.png", "url": "https://discord.test/ok"}
            ],
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

    def test_upload_discord_attachments_to_jira_reports_primary_url_failure_without_proxy_retry(
        self,
    ) -> None:
        client = MagicMock()
        calls: list[str] = []

        def _download(*, url: str) -> tuple[bytes, str | None]:
            calls.append(url)
            if "cdn.discordapp.com" in url:
                raise AtlassianOAuthError(
                    "HTTP 403 downloading attachment: error code: 1010"
                )
            return b"payload", "image/png"

        attachments = [
            {
                "url": "https://cdn.discordapp.com/attachments/test.png",
                "proxy_url": "https://media.discordapp.net/attachments/test.png",
                "filename": "test.png",
            }
        ]

        uploaded_count, warnings = upload_discord_attachments_to_jira(
            client=client,
            access_token="token",
            cloud_id="cloud",
            issue_key="MAB-1",
            attachments=attachments,
            download_attachment=_download,
        )

        self.assertEqual(uploaded_count, 0)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(calls[0], "https://cdn.discordapp.com/attachments/test.png")
        self.assertEqual(len(calls), 1)
        client.upload_issue_attachment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
