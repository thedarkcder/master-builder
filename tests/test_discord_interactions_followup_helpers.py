from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException
from orchestrator.api.discord.shared.errors import DiscordInteractionWebhookExpiredError
from orchestrator.tools.discord_api import DiscordApiError


class DiscordInteractionsFollowupHelpersTests(unittest.TestCase):
    def test_tenant_discord_channel_ids_and_project_channel_ids(self) -> None:
        from orchestrator.api.discord.interactions.followup import (
            _ask_thread_message_map_from_config,
            _project_channel_ids_for_tenant,
            _project_seed_followup_thread_channel_ids_for_tenant,
            _tenant_discord_channel_ids,
        )

        tenant = SimpleNamespace(discord_config={"channel_id": "tenant-chan"})
        merged = _tenant_discord_channel_ids(tenant=tenant, project_channel_ids={"project-chan"})
        self.assertEqual(merged, {"tenant-chan", "project-chan"})

        projects = [
            SimpleNamespace(discord_config={"channel_id": "p1", "ask_thread_channel_ids": ["a1", "a2"], "seed_followup_thread_channel_ids": ["s1"]}),
            SimpleNamespace(discord_config={"channel_id": "p2"}),
        ]
        session = MagicMock()
        session.execute.return_value.scalars.return_value.all.return_value = projects
        result = _project_channel_ids_for_tenant(session=session, tenant_id="t1")
        self.assertEqual(result, {"p1", "p2", "a1", "a2", "s1"})

        seed_only = _project_seed_followup_thread_channel_ids_for_tenant(session=session, tenant_id="t1")
        self.assertEqual(seed_only, {"s1"})
        self.assertEqual(
            _ask_thread_message_map_from_config(
                {"ask_thread_by_message_id": {"m1": "t1", "m2": " ", "": "t2"}}
            ),
            {"m1": "t1"},
        )

    def test_send_discord_interaction_followup_validation_and_http_error(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_interaction_followup

        with self.assertRaisesRegex(ValueError, "payload is incomplete"):
            _send_discord_interaction_followup(
                application_id=" ",
                interaction_token="x",
                content="hello",
            )

        error = HTTPError(
            url="https://discord.com",
            code=500,
            msg="boom",
            hdrs=None,
            fp=BytesIO(b"failed"),
        )
        with patch("orchestrator.api.discord.interactions.followup_transport.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "500"):
                _send_discord_interaction_followup(
                    application_id="app",
                    interaction_token="token",
                    content="hello",
                )

        expired_error = HTTPError(
            url="https://discord.com",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=BytesIO(b'{"message":"Unknown Webhook","code":10015}'),
        )
        with patch("orchestrator.api.discord.interactions.followup_transport.urlopen", side_effect=expired_error):
            with self.assertRaises(DiscordInteractionWebhookExpiredError):
                _send_discord_interaction_followup(
                    application_id="app",
                    interaction_token="token",
                    content="hello",
                )

    def test_send_discord_interaction_followup_includes_reply_reference_and_ephemeral_flag(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_interaction_followup

        captured = {}

        class _FakeOkResponse:
            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _fake_urlopen(request, timeout=30):  # noqa: ANN001
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeOkResponse()

        with patch("orchestrator.api.discord.interactions.followup_transport.urlopen", side_effect=_fake_urlopen):
            _send_discord_interaction_followup(
                application_id="app",
                interaction_token="token",
                content="hello",
                ephemeral=True,
                reply_to_message_id="m1",
                channel_id="c1",
            )

        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["flags"], 64)
        self.assertEqual(body["message_reference"]["message_id"], "m1")
        self.assertEqual(body["message_reference"]["channel_id"], "c1")

    def test_send_discord_interaction_followup_includes_components(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_interaction_followup

        captured = {}

        class _FakeOkResponse:
            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _fake_urlopen(request, timeout=30):  # noqa: ANN001
            captured["request"] = request
            captured["timeout"] = timeout
            return _FakeOkResponse()

        with patch("orchestrator.api.discord.interactions.followup_transport.urlopen", side_effect=_fake_urlopen):
            _send_discord_interaction_followup(
                application_id="app",
                interaction_token="token",
                content="hello",
                components=[{"type": 1}],
            )
        body = json.loads(captured["request"].data.decode("utf-8"))
        self.assertEqual(body["components"], [{"type": 1}])

    def test_resolve_thread_id_by_message_suffix_branches(self) -> None:
        from orchestrator.api.discord.interactions.followup import _resolve_thread_id_by_message_suffix

        client = MagicMock()
        self.assertIsNone(_resolve_thread_id_by_message_suffix(client=client, thread_ids=["t1"], message_id="   "))

        client.get_channel.side_effect = [ValueError("bad channel"), {"name": "thread-123456"}]
        result = _resolve_thread_id_by_message_suffix(
            client=client,
            thread_ids=["broken", "t2"],
            message_id="msg-123456",
        )
        self.assertEqual(result, "t2")
        client.get_channel.side_effect = [{"name": "thread-999999"}]
        self.assertIsNone(
            _resolve_thread_id_by_message_suffix(
                client=client,
                thread_ids=["t3"],
                message_id="msg-123456",
            )
        )

    def test_component_builders(self) -> None:
        from orchestrator.api.discord.interactions.followup import _ask_confirmation_components, _ask_reply_components

        with patch(
            "orchestrator.api.discord.interactions.followup.build_ask_confirmation_components",
            return_value=[{"id": "x"}],
        ) as confirmation_builder:
            self.assertEqual(_ask_confirmation_components("req-1"), [{"id": "x"}])
        confirmation_builder.assert_called_once_with("req-1")
        self.assertEqual(_ask_reply_components()[0]["components"][0]["custom_id"], "ask.reply.open")

    def test_resolve_thread_channel_for_reply_uses_message_mapping(self) -> None:
        from orchestrator.api.discord.interactions.followup import _resolve_thread_channel_for_reply

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1")
        project = SimpleNamespace(
            discord_config={
                "ask_thread_by_message_id": {
                    "message-1": "thread-1",
                }
            }
        )

        with (
            patch(
                "orchestrator.api.discord.interactions.followup_state.resolve_tenant_for_discord_channel",
                return_value=tenant,
            ),
            patch(
                "orchestrator.api.discord.interactions.followup_state.resolve_project_for_discord_channel",
                return_value=project,
            ),
        ):
            self.assertEqual(
                _resolve_thread_channel_for_reply(
                    session=session,
                    channel_id="parent-1",
                    reply_to_message_id="message-1",
                ),
                "thread-1",
            )

    def test_resolve_thread_channel_for_reply_falls_back_to_current_channel_without_mapping(self) -> None:
        from orchestrator.api.discord.interactions.followup import _resolve_thread_channel_for_reply

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1")
        project = SimpleNamespace(discord_config={})

        with (
            patch(
                "orchestrator.api.discord.interactions.followup_state.resolve_tenant_for_discord_channel",
                return_value=tenant,
            ),
            patch(
                "orchestrator.api.discord.interactions.followup_state.resolve_project_for_discord_channel",
                return_value=project,
            ),
        ):
            self.assertEqual(
                _resolve_thread_channel_for_reply(
                    session=session,
                    channel_id="parent-1",
                    reply_to_message_id="message-1",
                ),
                "parent-1",
            )

    def test_send_discord_thread_followup_paths(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_thread_followup

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"):
            client = MagicMock()
            with (
                patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=client),
                patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value={"thread-chan"}),
                patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            ):
                _send_discord_thread_followup(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="thread-chan",
                    reply_to_message_id="m1",
                    content="hello",
                )
            client.post_message.assert_called_once_with(channel_id="thread-chan", content="hello", components=None)

        with patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"):
            client = MagicMock()
            client.ensure_thread_for_message.side_effect = DiscordApiError("no thread")
            with (
                patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=client),
                patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
                patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            ):
                _send_discord_thread_followup(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="parent",
                    reply_to_message_id="m1",
                    content="hello",
                )
            self.assertGreaterEqual(client.post_message.call_count, 1)

    def test_send_discord_thread_followup_requires_bot_token_and_secret(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_thread_followup

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        with patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "is missing"):
                _send_discord_thread_followup(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="c1",
                    reply_to_message_id="m1",
                    content="hello",
                )

    def test_send_discord_thread_followup_uses_message_mapping(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_thread_followup

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        project = SimpleNamespace(
            discord_config={
                "ask_thread_channel_ids": ["thread-1"],
                "ask_thread_by_message_id": {"message-1": "thread-1"},
            },
            updated_at=None,
        )

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
            patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client_cls.return_value = client
            _send_discord_thread_followup(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id="parent-chan",
                reply_to_message_id="message-1",
                content="hello",
            )

        client.post_message.assert_called_once_with(channel_id="thread-1", content="hello", components=None)
        client.ensure_thread_for_message.assert_not_called()

    def test_send_discord_thread_followup_falls_back_when_mapped_thread_send_fails(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_thread_followup

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        project = SimpleNamespace(
            discord_config={
                "ask_thread_channel_ids": ["thread-1"],
                "ask_thread_by_message_id": {"message-1": "thread-1"},
            },
            updated_at=None,
        )

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
            patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client.post_message.side_effect = [
                DiscordApiError("mapped thread stale"),
                {"id": "final-message"},
            ]
            client.ensure_thread_for_message.return_value = "thread-2"
            client_cls.return_value = client
            _send_discord_thread_followup(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id="parent-chan",
                reply_to_message_id="message-1",
                content="hello",
            )

        self.assertEqual(client.post_message.call_count, 2)
        self.assertEqual(client.post_message.call_args_list[0].kwargs["channel_id"], "thread-1")
        self.assertEqual(client.post_message.call_args_list[1].kwargs["channel_id"], "thread-2")
        client.ensure_thread_for_message.assert_called_once()
        self.assertEqual(project.discord_config["ask_thread_by_message_id"]["message-1"], "thread-2")
        session.commit.assert_called_once()

    def test_send_discord_thread_followup_successfully_creates_and_persists_thread(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_thread_followup

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        project = SimpleNamespace(discord_config={"ask_thread_channel_ids": ["old-thread"]}, updated_at=None)

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
            patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client.ensure_thread_for_message.return_value = "new-thread"
            client_cls.return_value = client
            _send_discord_thread_followup(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id="parent",
                reply_to_message_id="message-1",
                content="hello",
            )

        self.assertIn("new-thread", project.discord_config["ask_thread_channel_ids"])
        self.assertEqual(project.discord_config["ask_thread_by_message_id"]["message-1"], "new-thread")
        session.commit.assert_called_once()
        client.post_message.assert_called_once_with(channel_id="new-thread", content="hello", components=None)

    def test_send_discord_thread_followup_recovers_existing_thread_by_name_suffix(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_thread_followup

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        project = SimpleNamespace(
            discord_config={
                "ask_thread_channel_ids": ["thread-legacy"],
            },
            updated_at=None,
        )

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
            patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client.ensure_thread_for_message.side_effect = DiscordApiError("already has thread")
            client.get_channel.return_value = {"name": "t1-ask-123456"}
            client_cls.return_value = client
            _send_discord_thread_followup(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id="parent-chan",
                reply_to_message_id="msg-000123456",
                content="hello",
            )

        client.post_message.assert_called_once_with(channel_id="thread-legacy", content="hello", components=None)
        self.assertEqual(project.discord_config["ask_thread_by_message_id"]["msg-000123456"], "thread-legacy")
        session.commit.assert_called_once()

    def test_send_discord_ask_response_with_thread_updates_project(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_ask_response_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        project = SimpleNamespace(discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "posted-1"}, {"id": "final-msg"}]
            client.create_thread_from_message.return_value = "thread-1"
            with patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=client):
                _send_discord_ask_response_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="channel-1",
                    user_id="u1",
                    content="content",
                )
        self.assertIn("thread-1", project.discord_config["ask_thread_channel_ids"])
        session.commit.assert_called_once()

    def test_send_discord_ask_response_with_thread_binds_issue_context(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_ask_response_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        project = SimpleNamespace(discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
            patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "posted-1"}, {"id": "final-msg"}]
            client.create_thread_from_message.return_value = "thread-1"
            client_cls.return_value = client
            _send_discord_ask_response_with_thread(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id="channel-1",
                user_id="u1",
                content="content",
                issue_key="MAB-159",
            )

        mapping = project.discord_config.get("decision_gate_thread_issue_by_channel_id", {})
        self.assertEqual(mapping.get("thread-1"), "MAB-159")
        session.commit.assert_called_once()

    def test_send_discord_ask_response_with_thread_requires_token_and_message_id(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_ask_response_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        with patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "is missing"):
                _send_discord_ask_response_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="c1",
                    user_id="u1",
                    content="content",
                )

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
            patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
        ):
            client = MagicMock()
            client.post_message.return_value = {}
            client_cls.return_value = client
            with self.assertRaisesRegex(RuntimeError, "did not include message ID"):
                _send_discord_ask_response_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="c1",
                    user_id="u1",
                    content="content",
                )

    def test_send_discord_ask_response_with_thread_reuses_existing_thread(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_ask_response_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch(
                "orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant",
                return_value={"thread-existing"},
            ),
        ):
            client = MagicMock()
            with patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=client):
                _send_discord_ask_response_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="thread-existing",
                    user_id="u1",
                    content="content",
                )

        client.post_message.assert_called_once()
        client.create_thread_from_message.assert_not_called()
        session.commit.assert_not_called()

    def test_send_discord_ask_response_with_thread_project_none_branch(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_ask_response_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=None),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "msg-1"}, {"id": "final"}]
            client.create_thread_from_message.return_value = "thread-1"
            client_cls.return_value = client
            _send_discord_ask_response_with_thread(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id="channel-1",
                user_id="u1",
                content="content",
            )

        session.commit.assert_called_once()

    def test_send_discord_seed_followup_with_thread_updates_followups(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_seed_followup_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(
            tenant_id="t1",
            discord_config={"seed_followups": [{"request_id": "req-1", "channel_ids": ["ch-0"]}]},
            updated_at=None,
        )
        project = SimpleNamespace(discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "seed-msg"}, {"id": "thread-msg"}]
            client.create_thread_from_message.return_value = "thread-2"
            with patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=client):
                _send_discord_seed_followup_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="ch-1",
                    user_id="u1",
                    content="seed content",
                    request_id="req-1",
                    questions=["q1", ""],
                )
        self.assertIn("thread-2", project.discord_config["seed_followup_thread_channel_ids"])
        self.assertIn("ch-1", tenant.discord_config["seed_followups"][0]["channel_ids"])
        self.assertIn("thread-2", tenant.discord_config["seed_followups"][0]["channel_ids"])
        session.commit.assert_called_once()

    def test_send_discord_seed_followup_with_thread_reuses_existing_thread(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_seed_followup_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch(
                "orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant",
                return_value={"seed-thread"},
            ),
        ):
            client = MagicMock()
            with patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=client):
                _send_discord_seed_followup_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="seed-thread",
                    user_id="u1",
                    content="seed content",
                    request_id="req-1",
                    questions=["q1", "q2"],
                )

        client.post_message.assert_called_once()
        client.create_thread_from_message.assert_not_called()
        session.commit.assert_not_called()

    def test_send_discord_seed_followup_with_thread_requires_token_and_message_id(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_seed_followup_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        with patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "is missing"):
                _send_discord_seed_followup_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="c1",
                    user_id="u1",
                    content="seed content",
                    request_id="req-1",
                    questions=["q1"],
                )

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
            patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=None),
        ):
            client = MagicMock()
            client.post_message.return_value = {}
            client_cls.return_value = client
            with self.assertRaisesRegex(RuntimeError, "did not include message ID"):
                _send_discord_seed_followup_with_thread(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    channel_id="c1",
                    user_id="u1",
                    content="seed content",
                    request_id="req-1",
                    questions=["q1"],
                )

    def test_send_discord_seed_followup_with_thread_additional_branches(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_seed_followup_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(
            tenant_id="t1",
            discord_config={"seed_followups": ["skip", {"request_id": "other", "channel_ids": ["c0"]}]},
            updated_at=None,
        )
        project = SimpleNamespace(
            discord_config={"seed_followup_thread_channel_ids": ["thread-2"]},
            updated_at=None,
        )
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
            patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient") as client_cls,
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "seed-msg"}, {"id": "thread-msg"}]
            client.create_thread_from_message.return_value = "thread-2"
            client_cls.return_value = client
            _send_discord_seed_followup_with_thread(
                session=session,
                settings=settings,
                tenant=tenant,
                channel_id="ch-1",
                user_id="u1",
                content="seed content",
                request_id="req-missing",
                questions=["q1"],
            )

        # Existing thread id branch (already present) should not duplicate.
        self.assertEqual(project.discord_config["seed_followup_thread_channel_ids"], ["thread-2"])
        # Non-dict and non-matching request entries are preserved.
        self.assertEqual(tenant.discord_config["seed_followups"][0], {"request_id": "other", "channel_ids": ["c0"]})
        session.commit.assert_called_once()

    def test_async_followup_wrappers_invoke_service(self) -> None:
        from orchestrator.api.discord.interactions.followup import (
            _run_discord_ask_confirmation_followup,
            _run_discord_command_followup,
        )

        service = MagicMock()
        service.run_discord_command_followup = AsyncMock()
        service.run_discord_ask_confirmation_followup = AsyncMock()
        build_service_mock = MagicMock(return_value=service)

        async def _run() -> None:
            with patch(
                "orchestrator.api.discord.interactions.followup.build_followup_service",
                build_service_mock,
            ):
                await _run_discord_command_followup(
                    tenant_id="t1",
                    user_id="u1",
                    channel_id="c1",
                    command_text="!ask",
                    application_id="app",
                    interaction_token="tok",
                )
                await _run_discord_ask_confirmation_followup(
                    tenant_id="t1",
                    user_id="u1",
                    channel_id="c1",
                    decision="approve",
                    request_id="req-1",
                    application_id="app",
                    interaction_token="tok",
                )

        asyncio.run(_run())
        self.assertTrue(service.run_discord_command_followup.called)
        self.assertTrue(service.run_discord_ask_confirmation_followup.called)

    def test_application_command_followup_parses_and_dispatches(self) -> None:
        from orchestrator.api.discord.interactions.followup import _run_discord_application_command_followup

        run_followup_mock = AsyncMock()
        with (
            patch(
                "orchestrator.api.discord.interactions.followup._parse_discord_interaction_command",
                return_value=("u1", "c1", "!ask status", {"issue_key": "MAB-135"}, []),
            ),
            patch(
                "orchestrator.api.discord.interactions.followup._run_discord_command_followup",
                run_followup_mock,
            ),
        ):
            asyncio.run(
                _run_discord_application_command_followup(
                    payload={"application_id": "app", "token": "tok", "data": {"name": "ask"}},
                    request_id="req-1",
                )
            )

        run_followup_mock.assert_awaited_once()
        self.assertEqual(run_followup_mock.call_args.kwargs["application_id"], "app")
        self.assertEqual(run_followup_mock.call_args.kwargs["interaction_token"], "tok")
        self.assertEqual(run_followup_mock.call_args.kwargs["command_text"], "!ask status")

    def test_application_command_followup_parse_error_sends_ephemeral_message(self) -> None:
        from orchestrator.api.discord.interactions.followup import _run_discord_application_command_followup

        with (
            patch(
                "orchestrator.api.discord.interactions.followup._parse_discord_interaction_command",
                side_effect=HTTPException(status_code=400, detail="bad command"),
            ),
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_interaction_followup",
            ) as send_followup_mock,
        ):
            asyncio.run(
                _run_discord_application_command_followup(
                    payload={"application_id": "app", "token": "tok", "data": {"name": "ask"}},
                    request_id="req-1",
                )
            )

        send_followup_mock.assert_called_once()
        self.assertEqual(send_followup_mock.call_args.kwargs["ephemeral"], True)
        self.assertIn("bad command", send_followup_mock.call_args.kwargs["content"])

    def test_async_followup_wrappers_resolve_tenant_by_channel_when_missing(self) -> None:
        from orchestrator.api.discord.interactions.followup import (
            _run_discord_ask_confirmation_followup,
            _run_discord_command_followup,
        )

        service = MagicMock()
        service.run_discord_command_followup = AsyncMock()
        service.run_discord_ask_confirmation_followup = AsyncMock()
        build_service_mock = MagicMock(return_value=service)

        with (
            patch("orchestrator.api.discord.interactions.followup.create_session_factory") as session_factory_mock,
            patch(
                "orchestrator.api.discord.interactions.followup.resolve_tenant_id_for_followup",
                return_value="t1",
            ),
            patch(
                "orchestrator.api.discord.interactions.followup.build_followup_service",
                build_service_mock,
            ),
        ):
            session_factory_mock.return_value.return_value = nullcontext(MagicMock())
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id=None,
                    user_id="u1",
                    channel_id="c1",
                    command_text="!ask",
                    application_id="app",
                    interaction_token="tok",
                )
            )
            asyncio.run(
                _run_discord_ask_confirmation_followup(
                    tenant_id=None,
                    user_id="u1",
                    channel_id="c1",
                    decision="approve",
                    request_id="r1",
                    application_id="app",
                    interaction_token="tok",
                )
            )

        self.assertEqual(service.run_discord_command_followup.call_args.kwargs["tenant_id"], "t1")
        self.assertEqual(service.run_discord_ask_confirmation_followup.call_args.kwargs["tenant_id"], "t1")

    def test_async_followup_wrappers_send_ephemeral_error_when_channel_unmapped(self) -> None:
        from orchestrator.api.discord.interactions.followup import (
            _run_discord_ask_confirmation_followup,
            _run_discord_command_followup,
        )

        with (
            patch("orchestrator.api.discord.interactions.followup.create_session_factory") as session_factory_mock,
            patch("orchestrator.api.discord.interactions.followup.resolve_tenant_for_discord_channel", return_value=None),
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup") as send_followup_mock,
        ):
            session_factory_mock.return_value.return_value = nullcontext(MagicMock())
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id=None,
                    user_id="u1",
                    channel_id="c1",
                    command_text="!ask",
                    application_id="app",
                    interaction_token="tok",
                )
            )
            asyncio.run(
                _run_discord_ask_confirmation_followup(
                    tenant_id=None,
                    user_id="u1",
                    channel_id="c1",
                    decision="approve",
                    request_id="r1",
                    application_id="app",
                    interaction_token="tok",
                )
            )

        self.assertEqual(send_followup_mock.call_count, 2)

    def test_run_discord_command_followup_reuses_existing_thread_channel(self) -> None:
        from orchestrator.api.discord.interactions.followup import _run_discord_command_followup

        tenant = SimpleNamespace(tenant_id="t1", is_enabled=True, jira_config={}, discord_config={}, updated_at=None)
        project = SimpleNamespace(discord_config={}, updated_at=None)
        session = MagicMock()
        session.get.return_value = tenant
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")
        command_response = SimpleNamespace(command="ask", message="Done", data={})

        with (
            patch("orchestrator.api.discord.interactions.followup.create_session_factory", return_value=lambda: nullcontext(session)),
            patch("orchestrator.api.discord.interactions.followup.get_settings", return_value=settings),
            patch("orchestrator.api.discord.interactions.followup.execute_discord_ingress_command", return_value=command_response),
            patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup_runtime.resolve_platform_secret_ref", return_value="token"),
            patch("orchestrator.core.discord.transport_executor.resolve_project_for_channel", return_value=project),
            patch(
                "orchestrator.core.discord.transport_executor.project_ask_thread_channel_ids_for_tenant",
                side_effect=[set(), {"thread-1"}],
            ),
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup") as send_interaction_followup_mock,
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "msg-1"}, None, None]
            client.create_thread_from_message.return_value = "thread-1"
            with patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=client):
                asyncio.run(
                    _run_discord_command_followup(
                        tenant_id="t1",
                        user_id="u1",
                        channel_id="channel-1",
                        command_text="!ask status",
                        application_id="app",
                        interaction_token="tok",
                    )
                )
                asyncio.run(
                    _run_discord_command_followup(
                        tenant_id="t1",
                        user_id="u1",
                        channel_id="thread-1",
                        command_text="!ask status",
                        application_id="app",
                        interaction_token="tok",
                    )
                )

        self.assertEqual(client.create_thread_from_message.call_count, 1)
        first_call_kwargs = client.create_thread_from_message.call_args.kwargs
        self.assertEqual(first_call_kwargs["channel_id"], "channel-1")
        self.assertEqual(first_call_kwargs["message_id"], "msg-1")
        self.assertIn("thread-1", project.discord_config["ask_thread_channel_ids"])
        self.assertGreaterEqual(client.post_message.call_count, 3)
        send_interaction_followup_mock.assert_not_called()

    def test_decision_gate_issue_for_thread_reads_project_mapping(self) -> None:
        from orchestrator.api.discord.interactions.followup import _decision_gate_issue_for_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="tenant-1")
        project = SimpleNamespace(
            discord_config={
                "decision_gate_thread_issue_by_channel_id": {
                    "thread-1": "mab-158",
                }
            }
        )
        with (
            patch("orchestrator.api.discord.interactions.followup_state.resolve_tenant_for_discord_channel", return_value=tenant),
            patch("orchestrator.api.discord.interactions.followup_state.resolve_project_for_discord_channel", return_value=project),
        ):
            self.assertEqual(
                _decision_gate_issue_for_thread(session=session, channel_id="thread-1"),
                ("tenant-1", "MAB-158"),
            )

    def test_decision_gate_issue_for_thread_falls_back_to_tenant_mapping(self) -> None:
        from orchestrator.api.discord.interactions.followup import _decision_gate_issue_for_thread

        session = MagicMock()
        tenant = SimpleNamespace(
            tenant_id="tenant-1",
            discord_config={
                "thread_issue_by_channel_id": {
                    "thread-2": "mab-200",
                }
            },
        )
        project = SimpleNamespace(discord_config={})
        with (
            patch("orchestrator.api.discord.interactions.followup_state.resolve_tenant_for_discord_channel", return_value=tenant),
            patch("orchestrator.api.discord.interactions.followup_state.resolve_project_for_discord_channel", return_value=project),
        ):
            self.assertEqual(
                _decision_gate_issue_for_thread(session=session, channel_id="thread-2"),
                ("tenant-1", "MAB-200"),
            )

    def test_decision_gate_reply_followup_rechecks_before_retry(self) -> None:
        from orchestrator.api.discord.interactions.followup import _run_discord_decision_gate_reply_followup_blocking

        with (
            patch("orchestrator.api.discord.interactions.followup._run_discord_command_followup_blocking") as followup_mock,
        ):
            _run_discord_decision_gate_reply_followup_blocking(
                tenant_id="tenant-1",
                user_id="u1",
                channel_id="thread-1",
                issue_key="MAB-158",
                reply_text="Objective: clear. How to test: pytest.",
                application_id="app",
                interaction_token="tok",
                reply_to_message_id="m1",
            )

        followup_mock.assert_called_once()
        kwargs = followup_mock.call_args.kwargs
        self.assertEqual(kwargs["tenant_id"], "tenant-1")
        self.assertEqual(kwargs["user_id"], "u1")
        self.assertEqual(kwargs["channel_id"], "thread-1")
        self.assertEqual(kwargs["command_text"], "!reply")
        self.assertEqual(kwargs["reply_to_message_id"], "m1")
        self.assertEqual(
            kwargs["command_params"],
            {
                "issue_key": "MAB-158",
                "reply_text": "Objective: clear. How to test: pytest.",
            },
        )


if __name__ == "__main__":
    unittest.main()
