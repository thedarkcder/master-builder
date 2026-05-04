from unittest.mock import patch

import pytest

from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.runtime.payload_models import AskIntent
from tests.test_support.discord_command_api_harness import DiscordCommandApiTestHarness


pytestmark = pytest.mark.contract


class DiscordAskPersonaCommandFlowTests(DiscordCommandApiTestHarness):
    def test_ask_command_requires_question(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !ask", response.json()["detail"])

    def test_engineer_command_returns_advisory_persona_response(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=("TP-20", "To Do", [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.personas.answer_voice_room_persona_with_runtime",
                return_value={
                    "message": "Split the work by persistence, API, and validation boundaries.",
                    "brief": {"focus": "decomposition"},
                },
            ) as answer_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!engineer @TP-20 how should we split this?",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "engineer")
        self.assertTrue(command_response.data["advisory_only"])
        self.assertEqual(command_response.data["persona_id"], "engineer")
        self.assertEqual(command_response.data["issue_key"], "TP-20")
        self.assertIn("persistence", command_response.message.lower())
        answer_mock.assert_called_once()

    def test_tester_command_maps_to_qa_persona(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.personas.answer_voice_room_persona_with_runtime",
                return_value={
                    "message": "Cover the happy path and one failed validation path.",
                    "brief": {},
                },
            ) as answer_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!tester what should we verify?",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "tester")
        self.assertEqual(command_response.data["persona_id"], "qa")
        self.assertEqual(answer_mock.call_args.kwargs["persona_id"], "qa")

    def test_ask_command_returns_board_answer(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"To Do": 2}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntent(mode="answer", summary="answer", command=None),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Board snapshot",
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!ask what is on the board",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["message"], "Board snapshot")
        self.assertEqual(response.json()["data"]["status_counts"]["To Do"], 2)

    def test_ask_command_supports_issue_scope(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=("TP-101", None, [], {}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntent(mode="answer", summary="answer", command=None),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Issue snapshot",
            ) as answer_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!ask @TP-101 summarize status",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["message"], "Issue snapshot")
        self.assertEqual(response.json()["data"]["issue_key"], "TP-101")
        self.assertEqual(answer_mock.call_args.kwargs["invocation_context"].issue_key, "TP-101")

    def test_ask_command_rejects_invalid_issue_scope_token(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask @bad summarize"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !ask @ISSUE-123", response.json()["detail"])
