import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.webhooks.followup_service import DiscordWebhookFollowupService
from orchestrator.core.observability import current_log_context, reset_log_context, set_log_context
from orchestrator.tools.discord_api import DiscordApiError


class DiscordWebhookFollowupServiceTests(unittest.TestCase):
    def _build_service(
        self,
        *,
        session,
        execute_command_ingress,
        ask_reply_components=None,
        consume_pending_ask_action=None,
        settings_factory=None,
    ) -> tuple[DiscordWebhookFollowupService, MagicMock]:
        reply_transport = MagicMock()
        service = DiscordWebhookFollowupService(
            session_factory=lambda: nullcontext(session),
            settings_factory=settings_factory or (lambda: SimpleNamespace()),
            execute_command_ingress=execute_command_ingress,
            command_request_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            build_command_followup_message=lambda **_kwargs: "formatted followup",
            ask_confirmation_components=lambda request_id: [{"type": 1, "request_id": request_id}],
            ask_reply_components=ask_reply_components or (lambda: [{"type": 1, "custom_id": "ask.reply.open"}]),
            reply_transport=reply_transport,
            consume_pending_ask_action=consume_pending_ask_action or (lambda **_kwargs: None),
        )
        return service, reply_transport

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

        transport.send_ask_with_thread.assert_called_once()
        transport.send_interaction_followup.assert_not_called()

    def test_command_followup_uses_thread_reply_transport_when_replying(self) -> None:
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
                reply_to_message_id="123456789012345678",
            )
        )

        transport.send_thread_reply.assert_called_once()
        transport.send_interaction_followup.assert_not_called()

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
        transport.send_thread_reply.side_effect = DiscordApiError("thread unavailable")

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

        transport.send_thread_reply.assert_called_once()
        transport.send_interaction_followup.assert_called_once()
        kwargs = transport.send_interaction_followup.call_args.kwargs
        self.assertEqual(kwargs["reply_to_message_id"], "123456789012345678")
        self.assertEqual(kwargs["channel_id"], "c-1")

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

        transport.send_interaction_followup.assert_called_once()
        sent_content = transport.send_interaction_followup.call_args.kwargs["content"]
        self.assertIn("ask confirmation payload was incomplete", sent_content)

    def test_command_followup_falls_back_to_reply_components_when_thread_send_fails(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(return_value=SimpleNamespace(command="ask", message="Done", data={}))
        service, transport = self._build_service(session=session, execute_command_ingress=execute)
        transport.send_ask_with_thread.side_effect = DiscordApiError("boom")

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

        transport.send_interaction_followup.assert_called_once()
        self.assertEqual(
            transport.send_interaction_followup.call_args.kwargs["components"],
            [{"type": 1, "custom_id": "ask.reply.open"}],
        )

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

        sent_content = transport.send_interaction_followup.call_args.kwargs["content"]
        self.assertIn("Command failed: bad request", sent_content)

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
        sent_content = transport.send_interaction_followup.call_args.kwargs["content"]
        self.assertIn("tenant is unavailable", sent_content)

    def test_command_followup_unexpected_error_emits_reference(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(side_effect=RuntimeError("explode"))
        service, transport = self._build_service(session=session, execute_command_ingress=execute)

        with patch("orchestrator.api.webhooks.followup_service.emit_hard_error") as emit_mock:
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
        sent_content = transport.send_interaction_followup.call_args.kwargs["content"]
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
        transport.send_interaction_followup.assert_not_called()

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

        transport.send_seed_with_thread.assert_called_once()
        questions = transport.send_seed_with_thread.call_args.kwargs["questions"]
        self.assertEqual(questions, ["Which issue key?"])
        transport.send_interaction_followup.assert_not_called()

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
        content = transport.send_interaction_followup.call_args.kwargs["content"]
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
        content = transport.send_interaction_followup.call_args.kwargs["content"]
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
        content = transport.send_interaction_followup.call_args.kwargs["content"]
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
        content = transport.send_interaction_followup.call_args.kwargs["content"]
        self.assertEqual(content, "formatted followup")


if __name__ == "__main__":
    unittest.main()
