from __future__ import annotations

import asyncio
import unittest
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.tools.discord_api import DiscordApiError


class DiscordInteractionsFollowupHelpersTests(unittest.TestCase):
    def test_tenant_discord_channel_ids_and_project_channel_ids(self) -> None:
        from orchestrator.api.discord.interactions.followup import (
            _project_channel_ids_for_tenant,
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
        with patch("orchestrator.api.discord.interactions.followup.urlopen", side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "500"):
                _send_discord_interaction_followup(
                    application_id="app",
                    interaction_token="token",
                    content="hello",
                )

    def test_send_discord_thread_followup_paths(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_thread_followup

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with patch("orchestrator.api.discord.interactions.followup.resolve_scoped_secret_ref", return_value="token"):
            client = MagicMock()
            with (
                patch("orchestrator.api.discord.interactions.followup.DiscordApiClient", return_value=client),
                patch("orchestrator.api.discord.interactions.followup._project_channel_ids_for_tenant", return_value={"thread-chan"}),
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

        with patch("orchestrator.api.discord.interactions.followup.resolve_scoped_secret_ref", return_value="token"):
            client = MagicMock()
            client.ensure_thread_for_message.side_effect = DiscordApiError("no thread")
            with (
                patch("orchestrator.api.discord.interactions.followup.DiscordApiClient", return_value=client),
                patch("orchestrator.api.discord.interactions.followup._project_channel_ids_for_tenant", return_value=set()),
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

    def test_send_discord_ask_response_with_thread_updates_project(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_ask_response_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        project = SimpleNamespace(discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_scoped_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "posted-1"}, {"id": "final-msg"}]
            client.create_thread_from_message.return_value = "thread-1"
            with patch("orchestrator.api.discord.interactions.followup.DiscordApiClient", return_value=client):
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

    def test_send_discord_ask_response_with_thread_reuses_existing_thread(self) -> None:
        from orchestrator.api.discord.interactions.followup import _send_discord_ask_response_with_thread

        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="t1", discord_config={}, updated_at=None)
        settings = SimpleNamespace(discord_bot_token_secret_ref="token/ref", secrets_encryption_key="enc")

        with (
            patch("orchestrator.api.discord.interactions.followup.resolve_scoped_secret_ref", return_value="token"),
            patch(
                "orchestrator.api.discord.interactions.followup._project_ask_thread_channel_ids_for_tenant",
                return_value={"thread-existing"},
            ),
        ):
            client = MagicMock()
            with patch("orchestrator.api.discord.interactions.followup.DiscordApiClient", return_value=client):
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
            patch("orchestrator.api.discord.interactions.followup.resolve_scoped_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup._resolve_project_for_channel", return_value=project),
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "seed-msg"}, {"id": "thread-msg"}]
            client.create_thread_from_message.return_value = "thread-2"
            with patch("orchestrator.api.discord.interactions.followup.DiscordApiClient", return_value=client):
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
            patch("orchestrator.api.discord.interactions.followup.resolve_scoped_secret_ref", return_value="token"),
            patch(
                "orchestrator.api.discord.interactions.followup._project_seed_followup_thread_channel_ids_for_tenant",
                return_value={"seed-thread"},
            ),
        ):
            client = MagicMock()
            with patch("orchestrator.api.discord.interactions.followup.DiscordApiClient", return_value=client):
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

    def test_async_followup_wrappers_invoke_service(self) -> None:
        from orchestrator.api.discord.interactions.followup import (
            _run_discord_ask_confirmation_followup,
            _run_discord_command_followup,
        )

        service = MagicMock()
        service.run_discord_command_followup = AsyncMock()
        service.run_discord_ask_confirmation_followup = AsyncMock()
        service_cls = MagicMock(return_value=service)

        async def _run() -> None:
            with patch("orchestrator.api.discord.interactions.followup.DiscordWebhookFollowupService", service_cls):
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
            patch("orchestrator.api.discord.interactions.followup.resolve_scoped_secret_ref", return_value="token"),
            patch("orchestrator.api.discord.interactions.followup.resolve_project_for_discord_channel", return_value=project),
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup") as send_interaction_followup_mock,
        ):
            client = MagicMock()
            client.post_message.side_effect = [{"id": "msg-1"}, None, None]
            client.create_thread_from_message.return_value = "thread-1"
            with patch("orchestrator.api.discord.interactions.followup.DiscordApiClient", return_value=client):
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


if __name__ == "__main__":
    unittest.main()
