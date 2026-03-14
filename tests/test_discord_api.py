from __future__ import annotations

import json
import unittest
from io import BytesIO
from urllib.error import HTTPError
from urllib.error import URLError
from unittest.mock import patch

from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError, DiscordTextChannel


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class DiscordApiClientTests(unittest.TestCase):
    def test_init_rejects_empty_token(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be empty"):
            DiscordApiClient(bot_token="   ")

    def test_request_json_parses_response(self) -> None:
        captured = {}

        def _fake_urlopen(request, timeout=30):  # noqa: ANN001, ARG001
            captured["method"] = request.get_method()
            captured["url"] = request.full_url
            captured["headers"] = dict(request.header_items())
            captured["body"] = request.data
            return _FakeResponse(b'{"ok":true}')

        client = DiscordApiClient(bot_token="token")
        with patch("orchestrator.tools.discord_api.urlopen", side_effect=_fake_urlopen):
            payload = client._request_json(method="POST", path="/channels/1/messages", payload={"content": "hello"})

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(captured["method"], "POST")
        self.assertIn("/channels/1/messages", str(captured["url"]))
        self.assertIn("Bot token", str(captured["headers"]))
        self.assertEqual(json.loads(captured["body"].decode("utf-8")), {"content": "hello"})

    def test_request_json_handles_cloudflare_1010(self) -> None:
        client = DiscordApiClient(bot_token="token")
        error = HTTPError(
            url="https://discord.com/api/v10/test",
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=BytesIO(b"Cloudflare Error 1010"),
        )
        with patch("orchestrator.tools.discord_api.urlopen", side_effect=error):
            with self.assertRaisesRegex(DiscordApiError, "Cloudflare"):
                client._request_json(method="GET", path="/test")

    def test_request_json_handles_generic_http_error(self) -> None:
        client = DiscordApiClient(bot_token="token")
        error = HTTPError(
            url="https://discord.com/api/v10/test",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=BytesIO(b"bad auth"),
        )
        with patch("orchestrator.tools.discord_api.urlopen", side_effect=error):
            with self.assertRaisesRegex(DiscordApiError, "401"):
                client._request_json(method="GET", path="/test")

    def test_request_json_handles_network_url_error(self) -> None:
        client = DiscordApiClient(bot_token="token")
        with patch("orchestrator.tools.discord_api.urlopen", side_effect=URLError("[Errno 8] nodename nor servname provided")):
            with self.assertRaisesRegex(DiscordApiError, "network"):
                client._request_json(method="GET", path="/test")

    def test_list_text_channels_filters_and_normalizes(self) -> None:
        client = DiscordApiClient(bot_token="token")
        with patch.object(
            client,
            "_request_json",
            return_value=[
                {"id": "c1", "type": 0, "name": "alpha", "parent_id": "p1"},
                {"id": "c2", "type": 0, "name": "beta", "parent_id": ""},
                {"id": "voice", "type": 2, "name": "voice"},
                {"type": 0, "name": "missing-id"},
            ],
        ):
            channels = client.list_text_channels(guild_id="g1")
        self.assertEqual(
            channels,
            [
                DiscordTextChannel(channel_id="c1", name="alpha", parent_id="p1"),
                DiscordTextChannel(channel_id="c2", name="beta", parent_id=None),
            ],
        )

    def test_create_text_channel_validates_response(self) -> None:
        client = DiscordApiClient(bot_token="token")
        with patch.object(client, "_request_json", return_value={"id": "c1", "name": "alpha", "parent_id": None}):
            channel = client.create_text_channel(guild_id="g1", name="alpha")
        self.assertEqual(channel.channel_id, "c1")

        with patch.object(client, "_request_json", return_value={"name": "alpha"}):
            with self.assertRaisesRegex(DiscordApiError, "missing id"):
                client.create_text_channel(guild_id="g1", name="alpha")

    def test_post_message_validates_and_trims(self) -> None:
        client = DiscordApiClient(bot_token="token")
        with patch.object(client, "_request_json", return_value={"id": "m1"}) as mock_request:
            result = client.post_message(channel_id="  c1 ", content=" hello ")
        self.assertEqual(result, {"id": "m1"})
        self.assertEqual(mock_request.call_args.kwargs["path"], "/channels/c1/messages")
        self.assertEqual(mock_request.call_args.kwargs["payload"]["content"], "hello")

        with self.assertRaisesRegex(ValueError, "channel ID"):
            client.post_message(channel_id=" ", content="x")
        with self.assertRaisesRegex(ValueError, "content"):
            client.post_message(channel_id="c1", content=" ")

    def test_get_channel_and_get_message_validate_types(self) -> None:
        client = DiscordApiClient(bot_token="token")
        with patch.object(client, "_request_json", return_value={"id": "c1"}):
            self.assertEqual(client.get_channel(channel_id="c1")["id"], "c1")

        with patch.object(client, "_request_json", return_value=[]):
            with self.assertRaisesRegex(DiscordApiError, "not an object"):
                client.get_channel(channel_id="c1")

        with patch.object(client, "_request_json", return_value={"id": "m1"}):
            self.assertEqual(client.get_message(channel_id="c1", message_id="m1")["id"], "m1")

        with self.assertRaisesRegex(ValueError, "message ID"):
            client.get_message(channel_id="c1", message_id=" ")

    def test_thread_creation_and_ensure_thread_for_message(self) -> None:
        client = DiscordApiClient(bot_token="token")
        with patch.object(client, "_request_json", return_value={"id": "t1"}) as mock_request:
            thread_id = client.create_thread_from_message(channel_id="c1", message_id="m1", name="Name")
        self.assertEqual(thread_id, "t1")
        self.assertIn("/channels/c1/messages/m1/threads", mock_request.call_args.kwargs["path"])

        with patch.object(client, "get_message", return_value={"thread": {"id": "t-existing"}}):
            self.assertEqual(
                client.ensure_thread_for_message(channel_id="c1", message_id="m1", thread_name="hello"),
                "t-existing",
            )

        with (
            patch.object(client, "get_message", return_value={"id": "m1"}),
            patch.object(client, "create_thread_from_message", return_value="t-new") as create_mock,
        ):
            self.assertEqual(
                client.ensure_thread_for_message(channel_id="c1", message_id="m1", thread_name="hello"),
                "t-new",
            )
            create_mock.assert_called_once()

    def test_dm_and_command_methods(self) -> None:
        client = DiscordApiClient(bot_token="token")
        with patch.object(client, "_request_json", return_value={"id": "dm1"}):
            self.assertEqual(client.create_dm_channel(user_id="u1"), "dm1")

        with (
            patch.object(client, "create_dm_channel", return_value="dm1"),
            patch.object(client, "post_message", return_value={"id": "m1"}) as post_mock,
        ):
            result = client.send_direct_message(user_id="u1", content="hello")
        self.assertEqual(result, {"id": "m1"})
        post_mock.assert_called_once_with(channel_id="dm1", content="hello")

        with patch.object(
            client,
            "list_text_channels",
            return_value=[DiscordTextChannel(channel_id="c1", name="proj", parent_id="p1")],
        ):
            existing = client.ensure_text_channel(guild_id="g1", name="proj", parent_id="p1")
        self.assertEqual(existing.channel_id, "c1")

        with (
            patch.object(client, "list_text_channels", return_value=[]),
            patch.object(client, "create_text_channel", return_value=DiscordTextChannel("c2", "new", None)) as create_mock,
        ):
            created = client.ensure_text_channel(guild_id="g1", name="new")
        self.assertEqual(created.channel_id, "c2")
        create_mock.assert_called_once()

        with patch.object(client, "_request_json", return_value={"id": "app1"}):
            self.assertEqual(client.get_application_id(), "app1")

        with patch.object(client, "_request_json", return_value={}):
            with self.assertRaisesRegex(DiscordApiError, "missing id"):
                client.get_application_id()

        with patch.object(client, "_request_json", return_value=[{"id": "1"}, "bad", {"id": "2"}]):
            commands = client.overwrite_guild_commands(application_id="app", guild_id="guild", commands=[])
        self.assertEqual(commands, [{"id": "1"}, {"id": "2"}])

        with patch.object(client, "_request_json", return_value={}):
            with self.assertRaisesRegex(DiscordApiError, "not a list"):
                client.overwrite_guild_commands(application_id="app", guild_id="guild", commands=[])


if __name__ == "__main__":
    unittest.main()
