from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.core.discord.commands_sync import sync_discord_guild_commands
from orchestrator.core.discord.gateway_listener import (
    DiscordGatewayListener,
    _decision_gate_issue_for_thread,
    _project_seed_followup_thread_ids,
)
from orchestrator.tools.discord_api import DiscordApiError


class _SessionCtx:
    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session

    def __enter__(self):  # noqa: ANN204
        return self._session

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class _SessionFactory:
    def __init__(self, session) -> None:  # noqa: ANN001
        self._session = session

    def __call__(self):  # noqa: ANN204
        return _SessionCtx(self._session)


class DiscordCommandSyncRuntimeTests(unittest.TestCase):
    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            discord_guild_id="guild-1",
            secrets_encryption_key="enc",
        )

    def test_sync_discord_guild_commands_guard_paths(self) -> None:
        settings = self._settings()
        resolver = MagicMock(return_value="")
        self.assertFalse(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=_SessionFactory(MagicMock()),
                secret_resolver=resolver,
            )
        )

        settings = self._settings()
        settings.discord_guild_id = ""
        resolver = MagicMock(side_effect=lambda _session, **kwargs: "token" if kwargs["secret_ref"] != "DISCORD_GUILD_ID" else "")
        self.assertFalse(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=_SessionFactory(MagicMock()),
                secret_resolver=resolver,
            )
        )

    def test_sync_discord_guild_commands_success_and_failure(self) -> None:
        settings = self._settings()
        session_factory = _SessionFactory(MagicMock())

        def _resolver(_session, *, secret_ref: str, encryption_key: str) -> str:  # noqa: ARG001
            if secret_ref == "DISCORD_BOT_TOKEN":
                return "bot-token"
            return "guild-from-secret"

        client = MagicMock()
        client.get_application_id.return_value = "app-1"
        client.overwrite_guild_commands.return_value = [{"name": "help"}]

        self.assertTrue(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=session_factory,
                secret_resolver=_resolver,
                client_factory=MagicMock(return_value=client),
            )
        )
        client.overwrite_guild_commands.assert_called_once()

        client_factory = MagicMock(side_effect=DiscordApiError("boom"))
        self.assertFalse(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=session_factory,
                secret_resolver=_resolver,
                client_factory=client_factory,
            )
        )


class DiscordGatewayListenerRuntimeTests(unittest.TestCase):
    def _listener(self) -> tuple[DiscordGatewayListener, MagicMock]:
        settings = SimpleNamespace(
            secrets_encryption_key="enc",
        )
        session = MagicMock()
        with patch("orchestrator.core.discord.gateway_listener.create_session_factory", return_value=_SessionFactory(session)):
            listener = DiscordGatewayListener(settings=settings)
        return listener, session

    def test_handle_message_create_ignores_bots_and_slash_commands(self) -> None:
        listener, _session = self._listener()

        with patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls:
            listener._handle_message_create(
                {"author": {"bot": True, "id": "u1"}, "channel_id": "c1", "content": "!run MAB-1"},
                bot_token="token",
            )
            listener._handle_message_create(
                {"author": {"id": "u1"}, "channel_id": "c1", "content": "/run"},
                bot_token="token",
            )

        client_cls.assert_not_called()

    def test_decision_gate_issue_for_thread_falls_back_to_tenant_mapping(self) -> None:
        session = MagicMock()
        project = SimpleNamespace(discord_config={})
        tenant = SimpleNamespace(
            discord_config={
                "thread_issue_by_channel_id": {
                    "thread-1": "gp-80",
                }
            }
        )
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        session.get.return_value = tenant

        self.assertEqual(
            _decision_gate_issue_for_thread(session=session, tenant_id="example", channel_id="thread-1"),
            "GP-80",
        )

    def test_handle_message_create_http_exception_and_internal_exception(self) -> None:
        listener, session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", side_effect=HTTPException(status_code=409, detail="blocked")),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {"author": {"id": "u1"}, "channel_id": "c1", "content": "!run MAB-1", "attachments": []},
                bot_token="token",
            )
        self.assertTrue(client_cls.return_value.post_message.called)
        args = client_cls.return_value.post_message.call_args.kwargs
        self.assertIn("Command failed: blocked", args["content"])

        client_cls.reset_mock()
        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", side_effect=RuntimeError("boom")),
            patch("orchestrator.core.discord.gateway_listener.emit_hard_error") as emit_mock,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient", return_value=client_cls.return_value),
        ):
            listener._handle_message_create(
                {"author": {"id": "u1"}, "channel_id": "c1", "content": "!run MAB-1", "attachments": []},
                bot_token="token",
            )
        self.assertTrue(emit_mock.called)
        error_content = client_cls.return_value.post_message.call_args.kwargs["content"]
        self.assertIn("internal error. Ref:", error_content)
        self.assertIs(session, session)

    def test_handle_message_create_seed_followup_rewrites_command_and_sets_components(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)

        command_response = SimpleNamespace(
            command="ask",
            message="Ready",
            data={"requires_confirmation": True, "request_id": "req-1"},
        )

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value={"thread-1"}),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={"thread-1": "GP"}),
            patch(
                "orchestrator.core.discord.gateway_listener.find_seed_followup_context",
                return_value={"request_id": "req-1"},
            ),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.build_ask_confirmation_components", return_value=[{"type": 1}]),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "thread-1",
                    "content": "follow up text",
                    "attachments": [{"id": "a1", "url": "https://x", "filename": "x.txt", "content_type": "text/plain", "size": 1}],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!issues followup follow up text")
        post_kwargs = client_cls.return_value.post_message.call_args.kwargs
        self.assertEqual(post_kwargs["components"], [{"type": 1}])

    def test_handle_message_create_seed_followup_thread_without_context_does_not_rewrite(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="ask", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value={"thread-1"}),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={"thread-1": "GP"}),
            patch("orchestrator.core.discord.gateway_listener.find_seed_followup_context", side_effect=[None, None]),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "thread-1",
                    "content": "follow up text",
                    "attachments": [],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "follow up text")

    def test_handle_message_create_seed_followup_thread_uses_user_project_fallback_context(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="ask", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value={"thread-1"}),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={"thread-1": "GP"}),
            patch(
                "orchestrator.core.discord.gateway_listener.find_seed_followup_context",
                side_effect=[None, {"request_id": "req-1", "project_key": "GP", "user_id": "u1"}],
            ),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "thread-1",
                    "content": "follow up text",
                    "attachments": [],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!issues followup follow up text")

    def test_handle_message_create_decision_gate_thread_routes_to_decision_gate_reply_command(self) -> None:
        listener, session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="reply", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._decision_gate_issue_for_thread", return_value="GP-80"),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "id": "m-1",
                    "author": {"id": "u1"},
                    "channel_id": "thread-1",
                    "content": "Objective and acceptance details",
                    "attachments": [],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!reply")
        self.assertEqual(payload.command_params["issue_key"], "GP-80")
        self.assertEqual(payload.command_params["reply_text"], "Objective and acceptance details")
        self.assertEqual(payload.command_params["source_ref"], "m-1")
        session.commit.assert_not_called()
        client_cls.return_value.post_message.assert_called_once()

    def test_handle_message_create_does_not_manage_session_when_command_fails(self) -> None:
        listener, session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch(
                "orchestrator.core.discord.gateway_listener.execute_tenant_discord_command",
                side_effect=HTTPException(status_code=409, detail="blocked"),
            ),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "id": "m-rollback",
                    "author": {"id": "u1"},
                    "channel_id": "thread-1",
                    "content": "reply text",
                    "attachments": [],
                },
                bot_token="token",
            )

        session.rollback.assert_not_called()
        client_cls.return_value.post_message.assert_called_once()

    def test_handle_message_create_routes_to_reply_using_real_thread_issue_mapping(self) -> None:
        listener, session = self._listener()
        tenant_for_channel = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant_for_channel)
        command_response = SimpleNamespace(command="reply", message="ok", data={})

        mapped_tenant = SimpleNamespace(
            discord_config={
                "thread_issue_by_channel_id": {
                    "thread-99": "gp-114",
                }
            }
        )
        project_without_mapping = SimpleNamespace(discord_config={})
        session.execute.return_value.scalars.return_value.all.return_value = [project_without_mapping]
        session.get.return_value = mapped_tenant

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "id": "m-2",
                    "author": {"id": "u1"},
                    "channel_id": "thread-99",
                    "content": "Dependencies: AVFoundation permissions and device latency",
                    "attachments": [],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!reply")
        self.assertEqual(payload.command_params["issue_key"], "GP-114")
        self.assertEqual(
            payload.command_params["reply_text"],
            "Dependencies: AVFoundation permissions and device latency",
        )
        client_cls.return_value.post_message.assert_called_once()

    def test_handle_message_create_non_seed_command_keeps_original_and_limits_attachments(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="run", message="ok", data={})
        attachments = [
            {"id": str(i), "url": f"https://file/{i}", "filename": f"f{i}.txt", "content_type": "text/plain", "size": i}
            for i in range(8)
        ]

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "channel-1",
                    "content": "!ask status",
                    "attachments": attachments,
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!ask status")
        self.assertEqual(len(payload.attachments), 5)
        client_cls.return_value.post_message.assert_called_once()

    def test_handle_message_create_discord_post_failure_is_swallowed(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="run", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response),
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            client_cls.return_value.post_message.side_effect = DiscordApiError("send failed")
            listener._handle_message_create(
                {"author": {"id": "u1"}, "channel_id": "channel-1", "content": "!run MAB-1", "attachments": []},
                bot_token="token",
            )

    def test_project_seed_followup_thread_ids_filters_archived_projects(self) -> None:
        active = SimpleNamespace(discord_config={"seed_followup_thread_channel_ids": ["t-1", "  ", None]})
        session = MagicMock()
        # The helper relies on DB query already filtering archived projects; feed only active result set.
        session.execute.return_value.scalars.return_value.all.return_value = [active]

        thread_ids = _project_seed_followup_thread_ids(session=session, tenant_id="example")
        self.assertEqual(thread_ids, {"t-1"})


if __name__ == "__main__":
    unittest.main()
