import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.webhooks.followup_service import DiscordWebhookFollowupService
from orchestrator.api.discord.shared.errors import DiscordInteractionWebhookExpiredError
from orchestrator.core.communications import (
    DiscordAskWithThreadAction,
    DiscordInteractionFollowupAction,
    DiscordSeedWithThreadAction,
    DiscordThreadReplyAction,
)
from orchestrator.core.observability import current_log_context, reset_log_context, set_log_context
from orchestrator.tools.discord_api import DiscordApiError


class RecordingExecutor:
    def __init__(self) -> None:
        self.actions: list[object] = []
        self._planned_failures: list[tuple[type[object], Exception]] = []

    def fail_once(self, action_type: type[object], exc: Exception) -> None:
        self._planned_failures.append((action_type, exc))

    def execute(self, *, action) -> None:  # noqa: ANN001
        self.actions.append(action)
        for index, (expected_type, exc) in enumerate(self._planned_failures):
            if isinstance(action, expected_type):
                self._planned_failures.pop(index)
                raise exc


class DiscordWebhookFollowupServiceTests(unittest.TestCase):
    def _build_service(
        self,
        *,
        session,
        execute_command_ingress,
        ask_reply_components=None,
        consume_pending_ask_action=None,
        settings_factory=None,
    ) -> tuple[DiscordWebhookFollowupService, RecordingExecutor]:
        transport_executor = RecordingExecutor()
        service = DiscordWebhookFollowupService(
            session_factory=lambda: nullcontext(session),
            settings_factory=settings_factory or (lambda: SimpleNamespace()),
            execute_command_ingress=execute_command_ingress,
            command_request_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            build_command_followup_message=lambda **_kwargs: "formatted followup",
            ask_confirmation_components=lambda request_id: [{"type": 1, "request_id": request_id}],
            ask_reply_components=ask_reply_components or (lambda: [{"type": 1, "custom_id": "ask.reply.open"}]),
            transport_executor=transport_executor,
            consume_pending_ask_action=consume_pending_ask_action or (lambda **_kwargs: None),
        )
        return service, transport_executor

    def test_command_followup_uses_ask_thread_transport_for_initial_ask(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="ask",
                message="Done",
                data={"issue_key": "example-46"},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!ask status?",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(transport.actions[0].issue_key, "example-46")

    def test_command_followup_passes_issue_key_to_thread_transport(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="run",
                message="Queued run",
                data={"issue_key": "MAB-159"},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!run MAB-159",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(transport.actions[0].issue_key, "MAB-159")

    def test_command_followup_uses_thread_reply_transport_when_replying(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="runs",
                message="Done",
                data={"issue_key": "example-46"},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!ask status?",
                application_id="app-1",
                interaction_token="token-1",
                reply_to_message_id="123456789012345678",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordThreadReplyAction)

    def test_command_followup_thread_reply_failure_falls_back_to_interaction_reply_reference(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="ask",
                message="Done",
                data={"issue_key": "example-46"},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)
        transport.fail_once(DiscordThreadReplyAction, DiscordApiError("thread unavailable"))

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!ask status?",
                application_id="app-1",
                interaction_token="token-1",
                reply_to_message_id="123456789012345678",
            )
        )

        self.assertEqual(len(transport.actions), 2)
        self.assertIsInstance(transport.actions[0], DiscordThreadReplyAction)
        self.assertIsInstance(transport.actions[1], DiscordInteractionFollowupAction)
        self.assertEqual(transport.actions[1].reply_to_message_id, "123456789012345678")
        self.assertEqual(transport.actions[1].channel_id, "c-1")

    def test_command_followup_unknown_interaction_webhook_falls_back_to_channel_send(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="runs",
                message="Done",
                data={"issue_key": "example-46"},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)
        transport.fail_once(DiscordAskWithThreadAction, DiscordApiError("thread unavailable"))
        transport.fail_once(DiscordInteractionFollowupAction, DiscordInteractionWebhookExpiredError("Unknown Webhook"))

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!runs",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 3)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertIsInstance(transport.actions[1], DiscordInteractionFollowupAction)
        self.assertIsInstance(transport.actions[2], DiscordThreadReplyAction)
        self.assertEqual(transport.actions[2].channel_id, "c-1")
        self.assertTrue(transport.actions[2].reply_to_message_id.startswith("interaction-"))

    def test_command_followup_ask_confirmation_incomplete_payload(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="ask",
                message="Needs approval",
                data={"requires_confirmation": True, "summary": "Review command"},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!ask please",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        sent_content = transport.actions[0].content
        self.assertIn("ask confirmation payload was incomplete", sent_content)

    def test_command_followup_ask_confirmation_uses_thread_transport_with_components(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="ask",
                message="Needs approval",
                data={
                    "requires_confirmation": True,
                    "summary": "Review command",
                    "request_id": "req-1",
                    "proposed_command": "!issues TEST-1",
                },
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!ask please",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(transport.actions[0].components, [{"type": 1, "request_id": "req-1"}])

    def test_command_followup_falls_back_to_reply_components_when_thread_send_fails(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(return_value=SimpleNamespace(command="ask", message="Done", data={}))
        service, transport = self._build_service(session=session, execute_command_ingress=execute)
        transport.fail_once(DiscordAskWithThreadAction, DiscordApiError("boom"))

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!ask status",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 2)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertIsInstance(transport.actions[1], DiscordInteractionFollowupAction)
        self.assertEqual(transport.actions[1].components, [{"type": 1, "custom_id": "ask.reply.open"}])

    def test_command_followup_http_exception_formats_detail(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(side_effect=HTTPException(status_code=400, detail="bad request"))
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!issues",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        sent_content = transport.actions[0].content
        self.assertIn("Command failed: bad request", sent_content)

    def test_command_followup_http_exception_preserves_issue_context_for_thread_binding(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(side_effect=HTTPException(status_code=409, detail="Good To Do still needs clarification"))
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!run GP-114",
                command_params={"issue_key": "gp-114"},
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(transport.actions[0].issue_key, "GP-114")

    def test_command_followup_http_exception_extracts_issue_key_from_command_text(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(side_effect=HTTPException(status_code=409, detail="Good To Do still needs clarification"))
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!run GP-118",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(transport.actions[0].issue_key, "GP-118")

    def test_command_followup_without_issue_context_binds_none(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="help",
                message="Usage",
                data={},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!help",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertIsNone(transport.actions[0].issue_key)

    def test_command_followup_response_issue_key_overrides_command_hint(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="run",
                message="Queued run",
                data={"issue_key": "GP-200"},
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!run GP-100",
                command_params={"issue_key": "gp-100"},
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        self.assertEqual(transport.actions[0].issue_key, "GP-200")

    def test_command_followup_disabled_tenant(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=False, tenant_id="tenant-1")
        execute = MagicMock()
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!ask",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        execute.assert_not_called()
        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordInteractionFollowupAction)
        sent_content = transport.actions[0].content
        self.assertIn("tenant is unavailable", sent_content)

    def test_command_followup_unexpected_error_emits_reference(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(side_effect=RuntimeError("explode"))
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        with patch("orchestrator.api.webhooks.followup_execution.emit_hard_error") as emit_mock:
            asyncio.run(
                service.run_discord_command_followup(
                    tenant_id="tenant-1",
                    user_id="u-1",
                    channel_id="c-1",
                    command_text="!ask",
                    application_id="app-1",
                    interaction_token="token-1",
                )
            )

        emit_mock.assert_called_once()
        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordAskWithThreadAction)
        sent_content = transport.actions[0].content
        self.assertIn("Ref:", sent_content)

    def test_command_followup_resets_log_context_when_settings_factory_fails(self) -> None:
        session = MagicMock()
        execute = MagicMock()

        def _fail_settings() -> SimpleNamespace:
            raise RuntimeError("settings unavailable")

        service, transport = self._build_service(
            session=session,
            execute_command_ingress=execute,
            settings_factory=_fail_settings,
        )
        parent_tokens = set_log_context(correlation_id="parent-cid", tenant_id="parent-tenant", agent_id="parent-agent")
        try:
            with self.assertRaises(RuntimeError):
                asyncio.run(
                    service.run_discord_command_followup(
                        tenant_id="tenant-1",
                        user_id="u-1",
                        channel_id="c-1",
                        command_text="!ask",
                        application_id="app-1",
                        interaction_token="token-1",
                    )
                )
            context = current_log_context()
            self.assertEqual(context["correlation_id"], "parent-cid")
            self.assertEqual(context["tenant_id"], "parent-tenant")
            self.assertEqual(context["agent_id"], "parent-agent")
        finally:
            reset_log_context(parent_tokens)
        execute.assert_not_called()
        self.assertEqual(transport.actions, [])

    def test_command_followup_issues_requires_input_creates_seed_thread(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="issues",
                message="Need clarification",
                data={
                    "requires_input": True,
                    "followup_request_id": "req-1",
                    "questions": ["Which issue key?", ""],
                },
            )
        )
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        asyncio.run(
            service.run_discord_command_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                command_text="!issues",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordSeedWithThreadAction)
        self.assertEqual(transport.actions[0].questions, ["Which issue key?"])

    def test_ask_confirmation_rejects_non_owner(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        pending = {"user_id": "u-owner", "channel_id": "c-1", "proposed_command": "!issues TEST-1"}
        execute = MagicMock()
        service, transport = self._build_service(
            session=session,
            execute_command_ingress=execute,
            consume_pending_ask_action=lambda **_kwargs: pending,
        )

        asyncio.run(
            service.run_discord_ask_confirmation_followup(
                tenant_id="tenant-1",
                user_id="u-other",
                channel_id="c-1",
                decision="approve",
                request_id="req-1",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        execute.assert_not_called()
        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordInteractionFollowupAction)
        content = transport.actions[0].content
        self.assertIn("Only the original requester", content)

    def test_ask_confirmation_reject_decision(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        pending = {"user_id": "u-1", "channel_id": "c-1", "proposed_command": "!issues TEST-1"}
        execute = MagicMock()
        service, transport = self._build_service(
            session=session,
            execute_command_ingress=execute,
            consume_pending_ask_action=lambda **_kwargs: pending,
        )

        asyncio.run(
            service.run_discord_ask_confirmation_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                decision="reject",
                request_id="req-1",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        execute.assert_not_called()
        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordInteractionFollowupAction)
        content = transport.actions[0].content
        self.assertIn("Action rejected", content)

    def test_ask_confirmation_blocks_recursive_ask(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        pending = {"user_id": "u-1", "channel_id": "c-1", "proposed_command": "!ask again"}
        execute = MagicMock()
        service, transport = self._build_service(
            session=session,
            execute_command_ingress=execute,
            consume_pending_ask_action=lambda **_kwargs: pending,
        )

        asyncio.run(
            service.run_discord_ask_confirmation_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                decision="approve",
                request_id="req-1",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        execute.assert_not_called()
        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordInteractionFollowupAction)
        content = transport.actions[0].content
        self.assertIn("recursive ask actions are not allowed", content)

    def test_ask_confirmation_blocks_recursive_ask_with_irregular_whitespace(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        pending = {"user_id": "u-1", "channel_id": "c-1", "proposed_command": "!   ask   again"}
        execute = MagicMock()
        service, transport = self._build_service(
            session=session,
            execute_command_ingress=execute,
            consume_pending_ask_action=lambda **_kwargs: pending,
        )

        asyncio.run(
            service.run_discord_ask_confirmation_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                decision="approve",
                request_id="req-1",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        execute.assert_not_called()
        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordInteractionFollowupAction)
        content = transport.actions[0].content
        self.assertIn("recursive ask actions are not allowed", content)

    def test_ask_confirmation_executes_and_formats_response(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        pending = {"user_id": "u-1", "channel_id": "c-1", "proposed_command": "!issues TEST-1"}
        execute = MagicMock(return_value=SimpleNamespace(command="issues", message="done", data={}))
        service, transport = self._build_service(
            session=session,
            execute_command_ingress=execute,
            consume_pending_ask_action=lambda **_kwargs: pending,
        )

        asyncio.run(
            service.run_discord_ask_confirmation_followup(
                tenant_id="tenant-1",
                user_id="u-1",
                channel_id="c-1",
                decision="approve",
                request_id="req-1",
                application_id="app-1",
                interaction_token="token-1",
            )
        )

        execute.assert_called_once()
        self.assertEqual(len(transport.actions), 1)
        self.assertIsInstance(transport.actions[0], DiscordInteractionFollowupAction)
        content = transport.actions[0].content
        self.assertEqual(content, "formatted followup")


if __name__ == "__main__":
    unittest.main()
