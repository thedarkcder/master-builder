from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.routes.webhook_discord_interactions import ingest_discord_interaction


class DiscordInteractionsRouteTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload: dict, *, headers: dict[str, str] | None = None, **overrides):
        request = SimpleNamespace(headers=headers or {})
        session = MagicMock()

        def _capture_and_close(coro):  # noqa: ANN001
            coro.close()
            return MagicMock()

        base = {
            "get_settings": MagicMock(return_value=SimpleNamespace()),
            "_read_json_payload": AsyncMock(return_value=(payload, b"{}")),
            "_resolve_discord_interactions_public_key": MagicMock(return_value=b"k"),
            "_validate_discord_interaction_signature": MagicMock(),
            "_find_tenant_for_discord_channel": MagicMock(return_value=SimpleNamespace(tenant_id="route25")),
            "_discord_autocomplete_response": MagicMock(side_effect=lambda choices: SimpleNamespace(status_code=200, body=b"auto")),
            "_find_focused_discord_option": MagicMock(return_value=("issue_key", "MAB")),
            "_discord_issue_autocomplete_choices": MagicMock(return_value=[{"name": "MAB-1", "value": "MAB-1"}]),
            "_discord_interaction_response": MagicMock(side_effect=lambda content, ephemeral=True: SimpleNamespace(status_code=200, body=str(content).encode())),
            "_discord_interaction_modal_response": MagicMock(side_effect=lambda **_: SimpleNamespace(status_code=200, body=b"modal")),
            "_discord_interaction_deferred_response": MagicMock(side_effect=lambda ephemeral=True: SimpleNamespace(status_code=200, body=b"deferred")),
            "_parse_ask_confirmation_custom_id": MagicMock(return_value=("approve", "req-1")),
            "_parse_ask_reply_modal_custom_id": MagicMock(return_value="m1"),
            "_discord_modal_text_value": MagicMock(return_value="next step"),
            "_run_discord_ask_confirmation_followup": AsyncMock(),
            "_run_discord_command_followup": AsyncMock(),
            "asyncio": SimpleNamespace(create_task=MagicMock(side_effect=_capture_and_close)),
            "_parse_discord_interaction_command": MagicMock(return_value=("u1", "c1", "!ask test", None, [])),
            "ASK_REPLY_OPEN_CUSTOM_ID": "ask.reply.open",
        }
        base.update(overrides)

        with ExitStack() as stack:
            for name, value in base.items():
                stack.enter_context(patch(f"orchestrator.api.routes.webhook_discord_interactions.{name}", value))
            return await ingest_discord_interaction(request=request, session=session)

    async def test_ping_and_unsupported_type(self) -> None:
        ping = await self._call({"type": 1})
        self.assertEqual(ping.status_code, 200)

        unsupported = await self._call({"type": 999})
        self.assertEqual(unsupported.status_code, 200)
        self.assertIn(b"Unsupported Discord interaction type", unsupported.body)

    async def test_autocomplete_paths(self) -> None:
        response = await self._call({"type": 4, "channel_id": "", "data": {}}, _find_tenant_for_discord_channel=MagicMock(return_value=None))
        self.assertEqual(response.status_code, 200)

        response = await self._call(
            {"type": 4, "channel_id": "c1", "data": {"name": "run", "options": []}},
            _find_focused_discord_option=MagicMock(return_value=None),
        )
        self.assertEqual(response.status_code, 200)

        response = await self._call(
            {"type": 4, "channel_id": "c1", "data": {"name": "status", "options": []}},
            _find_focused_discord_option=MagicMock(return_value=("issue_key", "MAB")),
        )
        self.assertEqual(response.status_code, 200)

        response = await self._call(
            {"type": 4, "channel_id": "c1", "data": {"name": "run", "options": []}},
            _discord_issue_autocomplete_choices=MagicMock(side_effect=HTTPException(status_code=403, detail="no")),
        )
        self.assertEqual(response.status_code, 200)

    async def test_message_component_paths(self) -> None:
        missing_channel = await self._call({"type": 3, "channel_id": ""})
        self.assertIn(b"Missing interaction channel_id", missing_channel.body)

        missing_context = await self._call({"type": 3, "channel_id": "c1", "token": "", "application_id": ""})
        self.assertIn(b"Missing Discord interaction context", missing_context.body)

        open_modal_missing_message = await self._call(
            {
                "type": 3,
                "channel_id": "c1",
                "application_id": "app",
                "token": "tok",
                "data": {"custom_id": "ask.reply.open"},
                "message": {},
            }
        )
        self.assertIn(b"Unable to open reply form", open_modal_missing_message.body)

        open_modal = await self._call(
            {
                "type": 3,
                "channel_id": "c1",
                "application_id": "app",
                "token": "tok",
                "data": {"custom_id": "ask.reply.open"},
                "message": {"id": "m1"},
            }
        )
        self.assertEqual(open_modal.status_code, 200)

        unsupported = await self._call(
            {
                "type": 3,
                "channel_id": "c1",
                "application_id": "app",
                "token": "tok",
                "data": {"custom_id": "x"},
            },
            _parse_ask_confirmation_custom_id=MagicMock(return_value=None),
        )
        self.assertIn(b"Unsupported interaction action", unsupported.body)

    async def test_modal_submit_paths(self) -> None:
        missing_data = await self._call({"type": 5, "channel_id": "c1", "application_id": "app", "token": "tok", "data": None})
        self.assertIn(b"Missing modal interaction data", missing_data.body)

        unsupported = await self._call(
            {"type": 5, "channel_id": "c1", "application_id": "app", "token": "tok", "data": {"custom_id": "x"}},
            _parse_ask_reply_modal_custom_id=MagicMock(return_value=""),
        )
        self.assertIn(b"Unsupported modal interaction", unsupported.body)

        missing_question = await self._call(
            {"type": 5, "channel_id": "c1", "application_id": "app", "token": "tok", "data": {"custom_id": "ask.reply.m1"}},
            _discord_modal_text_value=MagicMock(return_value=""),
        )
        self.assertIn(b"Please provide a follow-up question", missing_question.body)

    async def test_application_command_reply_and_command_paths(self) -> None:
        wrong_type = await self._call({"type": 2, "data": {"name": "reply", "type": 1}})
        self.assertIn(b"Reply is a message command", wrong_type.body)

        no_target = await self._call({"type": 2, "data": {"name": "reply", "type": 3, "target_id": ""}})
        self.assertIn(b"Reply target message was not provided", no_target.body)

        not_bot = await self._call(
            {
                "type": 2,
                "application_id": "app",
                "data": {
                    "name": "reply",
                    "type": 3,
                    "target_id": "m1",
                    "resolved": {"messages": {"m1": {"author": {"id": "other"}}}},
                },
            }
        )
        self.assertIn(b"Use Reply on a Master Builder message", not_bot.body)

        parse_error = await self._call(
            {"type": 2, "data": {"name": "ask"}},
            _parse_discord_interaction_command=MagicMock(side_effect=HTTPException(status_code=400, detail="bad command")),
        )
        self.assertIn(b"bad command", parse_error.body)

        ok = await self._call(
            {"type": 2, "application_id": "app", "token": "tok", "data": {"name": "ask"}},
            _parse_discord_interaction_command=MagicMock(return_value=("u1", "c1", "!ask test", None, [])),
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.body, b"deferred")


if __name__ == "__main__":
    unittest.main()
