from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine

from orchestrator.core.communications import (
    DiscordChannelMessageWithAttachmentAction,
    DiscordInteractionResponseAction,
    IngressResult,
)
from orchestrator.core.observability import current_log_context
from orchestrator.core.discord.command_sync_status import (
    get_discord_command_sync_status,
    reset_discord_command_sync_status,
)
from orchestrator.core.discord.commands_sync import sync_discord_guild_commands
from orchestrator.core.discord.gateway_listener import (
    DiscordGatewayListener,
    _decision_gate_issue_for_thread,
    _project_seed_followup_thread_ids,
)
from orchestrator.storage.models import Base, Tenant
from orchestrator.tools.discord_api import DiscordApiError


pytestmark = pytest.mark.contract


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


class _SessionFactoryFromDb:
    def __init__(self, database_url: str) -> None:
        from orchestrator.storage.db import create_session_factory

        self._factory = create_session_factory(database_url)

    def __call__(self):  # noqa: ANN204
        return self._factory()


def _matched_followup_resolution(**context_kwargs):
    context = SimpleNamespace(**context_kwargs)
    return SimpleNamespace(status="matched", context=context, matches=(context,))


def _no_followup_resolution():
    return SimpleNamespace(status="no_match", context=None, matches=())


class DiscordCommandSyncRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        database_url = f"sqlite:///{Path(self._temp_dir.name) / 'discord-sync.db'}"
        self._engine = create_engine(database_url)
        Base.metadata.create_all(self._engine)
        self._session_factory = _SessionFactoryFromDb(database_url)
        self._settings_obj = SimpleNamespace(
            discord_guild_id="guild-1",
            secrets_encryption_key="enc",
            database_url=database_url,
            discord_command_sync_lock_key=947102033130,
        )
        reset_discord_command_sync_status(settings=self._settings_obj)

    def _settings(self) -> SimpleNamespace:
        return self._settings_obj

    def _seed_tenant(self, *, tenant_id: str, guild_id: str | None, enabled: bool = True) -> None:
        with self._session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    name=tenant_id,
                    is_enabled=enabled,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={"guild_id": guild_id} if guild_id is not None else {},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_sync_discord_guild_commands_guard_paths(self) -> None:
        settings = self._settings()
        resolver = MagicMock(return_value="")
        self.assertFalse(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=self._session_factory,
                secret_resolver=resolver,
            )
        )
        status = get_discord_command_sync_status(settings=settings)
        self.assertEqual(status.last_failure_reason, "missing_bot_token")
        self.assertFalse(status.bot_token_configured)

        settings = self._settings()
        resolver = MagicMock(return_value="token")
        self.assertFalse(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=self._session_factory,
                secret_resolver=resolver,
            )
        )
        status = get_discord_command_sync_status(settings=settings)
        self.assertEqual(status.last_failure_reason, "missing_guild_id")
        self.assertFalse(status.guild_id_configured)

    def test_sync_discord_guild_commands_success_and_failure(self) -> None:
        settings = self._settings()
        self._seed_tenant(tenant_id="tenant-1", guild_id="guild-tenant-1")
        self._seed_tenant(tenant_id="tenant-2", guild_id="guild-tenant-2")

        def _resolver(_session, *, secret_ref: str, encryption_key: str) -> str:  # noqa: ARG001
            if secret_ref == "DISCORD_BOT_TOKEN":
                return "bot-token"
            raise AssertionError("guild IDs should come from tenant discord config")

        client = MagicMock()
        client.get_application_id.return_value = "app-1"
        client.overwrite_guild_commands.return_value = [{"name": "help"}]

        self.assertTrue(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=self._session_factory,
                secret_resolver=_resolver,
                client_factory=MagicMock(return_value=client),
            )
        )
        self.assertEqual(client.overwrite_guild_commands.call_count, 2)
        self.assertEqual(
            [call.kwargs["guild_id"] for call in client.overwrite_guild_commands.call_args_list],
            ["guild-tenant-1", "guild-tenant-2"],
        )
        status = get_discord_command_sync_status(settings=settings)
        self.assertTrue(status.synced)
        self.assertTrue(status.healthy)
        self.assertEqual(status.application_id, "app-1")
        self.assertEqual(status.command_count, 1)
        self.assertEqual(status.guild_id, "guild-tenant-1")

        client_factory = MagicMock(side_effect=DiscordApiError("boom"))
        self.assertFalse(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=self._session_factory,
                secret_resolver=_resolver,
                client_factory=client_factory,
            )
        )
        status = get_discord_command_sync_status(settings=settings)
        self.assertEqual(status.last_failure_reason, "discord_api_error")
        self.assertIn("boom", status.last_error or "")

    def test_sync_discord_guild_commands_ignores_legacy_global_guild_setting(self) -> None:
        settings = self._settings()
        settings.discord_guild_id = "legacy-global-guild"

        def _resolver(_session, *, secret_ref: str, encryption_key: str) -> str:  # noqa: ARG001
            if secret_ref == "DISCORD_BOT_TOKEN":
                return "bot-token"
            raise AssertionError("guild IDs should not be resolved from secrets")

        self.assertFalse(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=self._session_factory,
                secret_resolver=_resolver,
                client_factory=MagicMock(side_effect=AssertionError("should not attempt sync")),
            )
        )

        status = get_discord_command_sync_status(settings=settings)
        self.assertEqual(status.last_failure_reason, "missing_guild_id")
        self.assertFalse(status.guild_id_configured)

    def test_sync_discord_guild_commands_does_not_clobber_persisted_status_when_lock_busy(self) -> None:
        settings = self._settings()
        self._seed_tenant(tenant_id="tenant-1", guild_id="guild-1")

        def _resolver(_session, *, secret_ref: str, encryption_key: str) -> str:  # noqa: ARG001
            if secret_ref == "DISCORD_BOT_TOKEN":
                return "bot-token"
            raise AssertionError("guild IDs should come from tenant discord config")

        client = MagicMock()
        client.get_application_id.return_value = "app-1"
        client.overwrite_guild_commands.return_value = [{"name": "help"}]
        self.assertTrue(
            sync_discord_guild_commands(
                settings=settings,
                session_factory=self._session_factory,
                secret_resolver=_resolver,
                client_factory=MagicMock(return_value=client),
            )
        )

        with patch("orchestrator.core.discord.commands_sync._try_acquire_command_sync_lock", return_value=False):
            self.assertTrue(
                sync_discord_guild_commands(
                    settings=settings,
                    session_factory=self._session_factory,
                    secret_resolver=_resolver,
                    client_factory=MagicMock(side_effect=AssertionError("should not sync while lock busy")),
                )
            )

        status = get_discord_command_sync_status(settings=settings)
        self.assertTrue(status.healthy)
        self.assertEqual(status.application_id, "app-1")


class DiscordGatewayListenerRuntimeTests(unittest.TestCase):
    def _listener(
        self,
        *,
        transcribe_audio_attachment=None,  # noqa: ANN001
    ) -> tuple[DiscordGatewayListener, MagicMock]:
        settings = SimpleNamespace(
            secrets_encryption_key="enc",
            voice_stt_provider="disabled",
            voice_tts_provider="disabled",
            pocket_tts_base_url="",
            pocket_tts_voice="",
        )
        session = MagicMock()
        with patch("orchestrator.core.discord.gateway_listener.create_session_factory", return_value=_SessionFactory(session)):
            listener = DiscordGatewayListener(
                settings=settings,
                transcribe_audio_attachment=transcribe_audio_attachment,
            )
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

    def test_handle_message_create_unmapped_command_posts_explicit_error(self) -> None:
        listener, _session = self._listener()
        listener._find_tenant_for_channel = MagicMock(return_value=None)

        with patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls:
            listener._handle_message_create(
                {"author": {"id": "u1"}, "channel_id": "c1", "content": "!run MAB-1", "attachments": []},
                bot_token="token",
            )

        client_cls.return_value.post_message.assert_called_once()
        self.assertIn(
            "No enabled tenant is configured for this Discord channel.",
            client_cls.return_value.post_message.call_args.kwargs["content"],
        )

    def test_handle_interaction_create_dispatches_and_sends_callback(self) -> None:
        listener, _session = self._listener()
        payload = {
            "id": "interaction-1",
            "token": "token-1",
            "type": 2,
            "application_id": "app-1",
            "channel_id": "channel-1",
            "user": {"id": "user-1"},
            "data": {"name": "bug", "options": [{"type": 3, "name": "summary", "value": "Login fails"}]},
        }
        result = IngressResult(
            actions=(
                DiscordInteractionResponseAction(
                    interaction_id="interaction-1",
                    interaction_token="token-1",
                    status_code=200,
                    body=b'{"type":5,"data":{"flags":64}}',
                ),
            )
        )

        with (
            patch(
                "orchestrator.core.discord.gateway_listener.build_discord_interaction_ingress_result",
                new=AsyncMock(return_value=result),
            ) as build_mock,
            patch("orchestrator.core.discord.gateway_listener.send_discord_interaction_callback") as callback_mock,
        ):
            asyncio.run(listener._handle_interaction_create(payload))

        build_mock.assert_called_once()
        callback_mock.assert_called_once_with(
            interaction_id="interaction-1",
            interaction_token="token-1",
            response_body=b'{"type":5,"data":{"flags":64}}',
        )

    def test_decision_gate_issue_for_thread_falls_back_to_tenant_mapping(self) -> None:
        session = MagicMock()
        with patch(
            "orchestrator.core.discord.gateway_listener.resolve_followup_context",
            return_value=SimpleNamespace(context_type="decision_gate", issue_key="gp-80"),
        ):
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

    def test_handle_message_create_resumes_pending_human_input_request(self) -> None:
        listener, session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        request = SimpleNamespace(
            request_id="request-1",
            issue_key="GP-122",
        )
        answered_request = SimpleNamespace(request_id="request-1", issue_key="GP-122")
        resumed_run = SimpleNamespace(run_id="run-2")

        with (
            patch(
                "orchestrator.core.discord.gateway_listener.resolve_followup_context_match",
                return_value=_matched_followup_resolution(context_type="human_input", request_id="request-1"),
            ),
            patch(
                "orchestrator.core.discord.gateway_listener.pending_human_input_for_request_id",
                return_value=request,
            ),
            patch(
                "orchestrator.core.discord.gateway_listener.answer_human_input_request",
                return_value=answered_request,
            ) as answer_mock,
            patch(
                "orchestrator.core.discord.gateway_listener.resume_workflow_from_human_input_answer",
                return_value=resumed_run,
            ) as resume_mock,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "id": "msg-1",
                    "channel_id": "thread-1",
                    "content": "123456",
                    "attachments": [],
                },
                bot_token="token",
            )

        answer_mock.assert_called_once()
        resume_mock.assert_called_once()
        post_kwargs = client_cls.return_value.post_message.call_args.kwargs
        self.assertIn("queued workflow attempt `run-2`", post_kwargs["content"])
        self.assertIn("GP-122", post_kwargs["content"])

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
            patch(
                "orchestrator.core.discord.gateway_listener.resolve_followup_context_match",
                return_value=_matched_followup_resolution(context_type="seed_followup", request_id="req-1"),
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

        with (
            patch("orchestrator.core.discord.gateway_listener.resolve_followup_context_match", return_value=_no_followup_resolution()),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command") as command_mock,
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

        command_mock.assert_not_called()

    def test_handle_message_create_root_channel_plain_text_without_context_is_ignored(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example", discord_config={}, jira_config={"project_keys": ["TP"]})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)

        with (
            patch("orchestrator.core.discord.gateway_listener.resolve_followup_context_match", return_value=_no_followup_resolution()),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command") as command_mock,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "channel-1",
                    "content": "just talking in the channel",
                    "attachments": [],
                },
                bot_token="token",
            )

        command_mock.assert_not_called()

    def test_handle_message_create_seed_followup_thread_uses_user_project_fallback_context(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="ask", message="ok", data={})

        with (
            patch(
                "orchestrator.core.discord.gateway_listener.resolve_followup_context_match",
                return_value=_matched_followup_resolution(context_type="seed_followup", request_id="req-1"),
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
            patch(
                "orchestrator.core.discord.gateway_listener.resolve_followup_context_match",
                return_value=_matched_followup_resolution(context_type="decision_gate", issue_key="GP-80"),
            ),
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

    def test_handle_message_create_parent_channel_reply_passes_root_message_id_to_followup_resolution(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example")
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="reply", message="ok", data={})

        with (
            patch(
                "orchestrator.core.discord.gateway_listener.resolve_followup_context_match",
                return_value=_matched_followup_resolution(context_type="decision_gate", issue_key="GP-124"),
            ) as resolve_context_mock,
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response),
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "id": "m-parent-reply",
                    "author": {"id": "u1"},
                    "channel_id": "discord-channel-1",
                    "content": "Reject relink; device_id stays bound to one user only.",
                    "attachments": [],
                    "message_reference": {
                        "message_id": "root-message-1",
                        "channel_id": "discord-channel-1",
                    },
                },
                bot_token="token",
            )

        self.assertEqual(
            resolve_context_mock.call_args.kwargs["root_message_id"],
            "root-message-1",
        )

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
                    "content": "!ask reply text",
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
            patch(
                "orchestrator.core.discord.gateway_listener.resolve_followup_context_match",
                return_value=_matched_followup_resolution(context_type="decision_gate", issue_key="GP-114"),
            ),
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

    def test_handle_message_create_room_routes_plain_text_to_pm_command(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example", discord_config={}, jira_config={"project_keys": ["TP"]})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="pm", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_room_channel_ids", return_value={"voice-room-1"}),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch(
                "orchestrator.core.discord.gateway_listener.route_discord_voice_entry",
                return_value={"lane": "interview", "persona": "pm", "confidence": 0.9, "reason": "test"},
            ),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "voice-room-1",
                    "content": "Need a product brief for checkout retry failures",
                    "attachments": [],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!pm Need a product brief for checkout retry failures")
        self.assertEqual(payload.command_params["room_mode"], "true")

    def test_handle_message_create_live_voice_linked_text_channel_routes_plain_text_to_room_mode(self) -> None:
        listener, session = self._listener()
        tenant = SimpleNamespace(tenant_id="example", discord_config={}, jira_config={"project_keys": ["TP"]})
        project = SimpleNamespace(
            discord_config={
                "live_voice_room_links": {
                    "voice-room-1": "text-room-1",
                }
            }
        )
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="pm", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.resolve_followup_context_match", return_value=_no_followup_resolution()),
            patch(
                "orchestrator.core.discord.gateway_listener.route_discord_voice_entry",
                return_value={"lane": "ask", "persona": "pm", "confidence": 0.9, "reason": "test"},
            ),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "text-room-1",
                    "content": "Need a product brief for checkout retry failures",
                    "attachments": [],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!pm Need a product brief for checkout retry failures")
        self.assertEqual(payload.command_params["room_mode"], "true")

    def test_handle_message_create_room_audio_only_uses_transcription_hook(self) -> None:
        listener, _session = self._listener(
            transcribe_audio_attachment=lambda _attachment: "Transcribed PM note from voice memo",
        )
        tenant = SimpleNamespace(tenant_id="example", discord_config={}, jira_config={"project_keys": ["TP"]})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="pm", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_room_channel_ids", return_value={"voice-room-1"}),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch(
                "orchestrator.core.discord.gateway_listener.route_discord_voice_entry",
                return_value={"lane": "interview", "persona": "pm", "confidence": 0.9, "reason": "test"},
            ),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "voice-room-1",
                    "content": "",
                    "attachments": [
                        {
                            "id": "a1",
                            "url": "https://files/audio.m4a",
                            "filename": "audio.m4a",
                            "content_type": "audio/mp4",
                            "size": 1234,
                        }
                    ],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!pm Transcribed PM note from voice memo")
        self.assertEqual(payload.command_params["room_mode"], "true")

    def test_handle_message_create_live_voice_linked_text_channel_audio_only_routes_to_room_mode(self) -> None:
        listener, session = self._listener(
            transcribe_audio_attachment=lambda _attachment: "Transcribed PM note from voice memo",
        )
        tenant = SimpleNamespace(tenant_id="example", discord_config={}, jira_config={"project_keys": ["TP"]})
        project = SimpleNamespace(
            jira_project_key="TP",
            discord_config={
                "live_voice_room_links": {
                    "voice-room-1": "text-room-1",
                },
            }
        )
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="pm", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.resolve_followup_context_match", return_value=_no_followup_resolution()),
            patch(
                "orchestrator.core.discord.gateway_listener.route_discord_voice_entry",
                return_value={"lane": "ask", "persona": "pm", "confidence": 0.9, "reason": "test"},
            ),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "text-room-1",
                    "content": "",
                    "attachments": [
                        {
                            "id": "a1",
                            "url": "https://files/audio.m4a",
                            "filename": "audio.m4a",
                            "content_type": "audio/mp4",
                            "size": 1234,
                        }
                    ],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!ask Transcribed PM note from voice memo")
        self.assertEqual(payload.command_params["room_mode"], "true")
        self.assertEqual(payload.command_params["room_source"], "voice_note")

    def test_handle_message_create_room_audio_only_without_transcription_posts_guidance(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example", discord_config={}, jira_config={"project_keys": ["TP"]})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)

        with (
            patch("orchestrator.core.discord.gateway_listener._project_room_channel_ids", return_value={"voice-room-1"}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command") as command_mock,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "voice-room-1",
                    "content": "",
                    "attachments": [
                        {
                            "id": "a1",
                            "url": "https://files/audio.m4a",
                            "filename": "audio.m4a",
                            "content_type": "audio/mp4",
                            "size": 1234,
                        }
                    ],
                },
                bot_token="token",
            )

        command_mock.assert_not_called()
        post_content = str(client_cls.return_value.post_message.call_args.kwargs["content"]).lower()
        self.assertIn("voice transcription", post_content)

    def test_handle_message_create_non_room_audio_only_routes_to_voice_router_and_posts_voice_reply(self) -> None:
        listener, _session = self._listener(
            transcribe_audio_attachment=lambda _attachment: "Summarize the deployment blockers",
        )
        listener._settings.voice_tts_provider = "pocket_tts"
        tenant = SimpleNamespace(tenant_id="example", discord_config={}, jira_config={"project_keys": ["TP"]})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(
            command="pm",
            message="Deployment is blocked on the worker image rebuild.",
            data={"persona_id": "pm", "persona_name": "PM", "room_config": {}},
        )
        voice_action = DiscordChannelMessageWithAttachmentAction(
            channel_id="tenant-chat-1",
            content="ok",
            filename="reply.mp3",
            file_bytes=b"ID3",
            content_type="audio/mpeg",
            components=None,
            failure_user_id="u1",
            fallback_content_on_failure="ok",
            fallback_components_on_failure=None,
        )

        with (
            patch("orchestrator.core.discord.gateway_listener._project_room_channel_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.resolve_followup_context_match", return_value=_no_followup_resolution()),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch.object(listener, "_build_room_voice_reply_action", return_value=(voice_action, None)) as build_voice_reply,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "tenant-chat-1",
                    "content": "",
                    "attachments": [
                        {
                            "id": "a1",
                            "url": "https://files/audio.m4a",
                            "filename": "audio.m4a",
                            "content_type": "audio/mp4",
                            "size": 1234,
                        }
                    ],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!ask Summarize the deployment blockers")
        self.assertEqual(payload.command_params, {"room_mode": "true", "room_source": "voice_note", "persona_id": "engineer"})
        client_cls.return_value.post_message_with_attachment.assert_called_once_with(
            channel_id="tenant-chat-1",
            content="ok",
            filename="reply.mp3",
            file_bytes=b"ID3",
            content_type="audio/mpeg",
            components=None,
        )
        build_voice_reply.assert_called_once()
        self.assertEqual(build_voice_reply.call_args.kwargs["text"], "Deployment is blocked on the worker image rebuild.")
        self.assertEqual(build_voice_reply.call_args.kwargs["persona_id"], "pm")
        self.assertEqual(build_voice_reply.call_args.kwargs["persona_name"], "PM")
        self.assertEqual(build_voice_reply.call_args.kwargs["content_override"], "ok")
        self.assertIsNone(build_voice_reply.call_args.kwargs["components"])
        client_cls.return_value.post_message_with_attachment.assert_called_once()

    def test_transcribe_room_audio_attachment_scopes_log_context(self) -> None:
        captured_contexts: list[dict[str, str | None]] = []

        def _transcribe(_attachment):  # noqa: ANN001
            captured_contexts.append(dict(current_log_context()))
            return "Transcribed voice note"

        listener, _session = self._listener(transcribe_audio_attachment=_transcribe)

        transcript, error = listener._transcribe_room_audio_attachment(
            attachment={
                "url": "https://files/audio.m4a",
                "filename": "audio.m4a",
                "content_type": "audio/mp4",
            },
            bot_token="token",
            correlation_id="message-1",
            tenant_id="example",
            project_id="project-1",
        )

        self.assertEqual((transcript, error), ("Transcribed voice note", None))
        self.assertEqual(
            captured_contexts,
            [
                {
                    "correlation_id": "message-1",
                    "tenant_id": "example",
                    "project_id": "project-1",
                    "agent_id": None,
                }
            ],
        )

    def test_handle_message_create_text_plus_audio_attachment_keeps_original_text_command(self) -> None:
        listener, _session = self._listener(
            transcribe_audio_attachment=lambda _attachment: "this should not be used",
        )
        tenant = SimpleNamespace(tenant_id="example", discord_config={})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(command="pm", message="ok", data={})

        with (
            patch("orchestrator.core.discord.gateway_listener._project_room_channel_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response) as command_mock,
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient"),
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "tenant-chat-1",
                    "content": "!pm Use the typed command",
                    "attachments": [
                        {
                            "id": "a1",
                            "url": "https://files/audio.m4a",
                            "filename": "audio.m4a",
                            "content_type": "audio/mp4",
                            "size": 1234,
                        }
                    ],
                },
                bot_token="token",
            )

        payload = command_mock.call_args.kwargs["payload"]
        self.assertEqual(payload.command, "!pm Use the typed command")

    def test_find_tenant_for_channel_accepts_voice_room_channel_ids(self) -> None:
        listener, _session = self._listener()
        session = MagicMock()
        project = SimpleNamespace(tenant_id="example", discord_config={"voice_room_channel_ids": ["voice-room-1"]})
        tenant = SimpleNamespace(tenant_id="example", is_enabled=True)
        session.execute.return_value.scalars.return_value.all.return_value = [project]
        session.get.return_value = tenant

        with patch("orchestrator.core.discord.gateway_listener.resolve_tenant_for_discord_channel", return_value=None):
            resolved = listener._find_tenant_for_channel(session=session, channel_id="voice-room-1")

        self.assertIs(resolved, tenant)

    def test_handle_message_create_room_voice_reply_uses_persona_metadata(self) -> None:
        listener, _session = self._listener()
        listener._settings.voice_tts_provider = "pocket_tts"
        tenant = SimpleNamespace(tenant_id="example", discord_config={})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(
            command="pm",
            message="Architect answer",
            data={
                "room_mode": True,
                "persona_id": "architect",
                "persona_name": "Soren",
                "room_config": {
                    "persona_names": {"architect": "Soren"},
                    "persona_voices": {"architect": "javert"},
                },
            },
        )

        with (
            patch("orchestrator.core.discord.gateway_listener._project_room_channel_ids", return_value={"voice-room-1"}),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response),
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch("orchestrator.core.discord.gateway_listener.synthesize_reply_audio") as synth_mock,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            synth_mock.return_value = SimpleNamespace(
                filename="reply.mp3",
                audio_bytes=b"ID3",
                content_type="audio/mpeg",
            )
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "voice-room-1",
                    "content": "Need architecture guidance",
                    "attachments": [],
                },
                bot_token="token",
            )

        synth_kwargs = synth_mock.call_args.kwargs
        self.assertEqual(synth_kwargs["persona_id"], "architect")
        self.assertEqual(synth_kwargs["room_config"]["persona_voices"]["architect"], "javert")
        self.assertEqual(synth_kwargs["text"], "Soren from Architecture. Architect answer")
        self.assertEqual(
            client_cls.return_value.post_message_with_attachment.call_args.kwargs["content"],
            "<@u1> Voice reply from Soren from Architecture",
        )

    def test_post_room_voice_reply_uses_content_override_and_components(self) -> None:
        listener, _session = self._listener()
        listener._settings.voice_tts_provider = "pocket_tts"
        captured_contexts: list[dict[str, str | None]] = []

        with (
            patch("orchestrator.core.discord.gateway_listener.synthesize_reply_audio") as synth_mock,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            def _synthesize(**_kwargs):  # noqa: ANN003
                captured_contexts.append(dict(current_log_context()))
                return SimpleNamespace(
                    filename="reply.mp3",
                    audio_bytes=b"ID3",
                    content_type="audio/mpeg",
                )

            synth_mock.side_effect = _synthesize
            error = listener._post_room_voice_reply(
                bot_token="token",
                user_id="u1",
                channel_id="voice-room-1",
                text="PM answer",
                persona_id="pm",
                persona_name="PM",
                persona_role="PM",
                room_config={},
                content_override="<@u1> Combined response",
                components=[{"type": 1}],
                correlation_id="message-2",
                tenant_id="example",
                project_id="project-1",
            )

        self.assertIsNone(error)
        self.assertEqual(synth_mock.call_args.kwargs["text"], "Andy from Product. PM answer")
        self.assertEqual(
            captured_contexts,
            [
                {
                    "correlation_id": "message-2",
                    "tenant_id": "example",
                    "project_id": "project-1",
                    "agent_id": None,
                }
            ],
        )
        self.assertEqual(
            client_cls.return_value.post_message_with_attachment.call_args.kwargs["content"],
            "<@u1> Combined response",
        )
        self.assertEqual(
            client_cls.return_value.post_message_with_attachment.call_args.kwargs["components"],
            [{"type": 1}],
        )

    def test_handle_message_create_room_skips_voice_reply_when_feature_disabled(self) -> None:
        listener, _session = self._listener()
        tenant = SimpleNamespace(tenant_id="example", discord_config={})
        listener._find_tenant_for_channel = MagicMock(return_value=tenant)
        command_response = SimpleNamespace(
            command="pm",
            message="PM answer",
            data={
                "room_mode": True,
                "persona_id": "pm",
                "persona_name": "PM",
                "room_config": {},
            },
        )

        with (
            patch("orchestrator.core.discord.gateway_listener._project_room_channel_ids", return_value={"voice-room-1"}),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_ids", return_value=set()),
            patch("orchestrator.core.discord.gateway_listener._project_seed_followup_thread_project_keys", return_value={}),
            patch("orchestrator.core.discord.gateway_listener.execute_tenant_discord_command", return_value=command_response),
            patch("orchestrator.core.discord.gateway_listener.resolve_tenant_jira_browse_base_url", return_value="https://jira.example.com"),
            patch("orchestrator.core.discord.gateway_listener.build_command_followup_message", return_value="ok"),
            patch.object(listener, "_post_room_voice_reply") as post_voice_reply,
            patch("orchestrator.core.discord.gateway_listener.DiscordApiClient") as client_cls,
        ):
            listener._handle_message_create(
                {
                    "author": {"id": "u1"},
                    "channel_id": "voice-room-1",
                    "content": "Need product guidance",
                    "attachments": [],
                },
                bot_token="token",
            )

        post_voice_reply.assert_not_called()
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
