import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.webhooks.followup_service import DiscordWebhookFollowupService


class DiscordWebhookFollowupServiceTests(unittest.TestCase):
    def _build_service(
        self,
        *,
        session,
        execute_command_ingress,
        ask_reply_components=None,
    ) -> tuple[DiscordWebhookFollowupService, MagicMock]:
        reply_transport = MagicMock()
        service = DiscordWebhookFollowupService(
            session_factory=lambda: nullcontext(session),
            settings_factory=lambda: SimpleNamespace(),
            execute_command_ingress=execute_command_ingress,
            command_request_factory=lambda **kwargs: SimpleNamespace(**kwargs),
            build_command_followup_message=lambda **_kwargs: "formatted followup",
            ask_confirmation_components=lambda request_id: [{"type": 1, "request_id": request_id}],
            ask_reply_components=ask_reply_components or (lambda: [{"type": 1, "custom_id": "ask.reply.open"}]),
            reply_transport=reply_transport,
            consume_pending_ask_action=lambda **_kwargs: None,
        )
        return service, reply_transport

    def test_command_followup_uses_ask_thread_transport_for_initial_ask(self) -> None:
        session = MagicMock()
        session.get.return_value = SimpleNamespace(is_enabled=True, tenant_id="tenant-1")
        execute = MagicMock(
            return_value=SimpleNamespace(
                command="ask",
                message="Done",
                data={"issue_key": "YANA-46"},
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
                data={"issue_key": "YANA-46"},
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


if __name__ == "__main__":
    unittest.main()
