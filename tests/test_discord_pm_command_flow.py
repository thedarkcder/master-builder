import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.storage.models import Tenant
from tests.test_support.discord_command_api_harness import DiscordCommandApiTestHarness


pytestmark = pytest.mark.contract


class DiscordPmCommandFlowTests(DiscordCommandApiTestHarness):
    def test_pm_command_requires_question(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!pm"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !pm", response.json()["detail"])

    def test_pm_command_returns_product_first_answer_and_stores_minimal_history(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What kind of rollout narrative do you need?\n"
                        "Examples: executive launch summary, customer-facing changelog, or support handoff."
                    ),
                    "brief": {
                        "objective": "Improve checkout recovery",
                        "user_value": "Customers recover from checkout failures more clearly.",
                        "target_user": "",
                        "primary_journey": "",
                        "acceptance_criteria": [],
                        "ui_references": [],
                        "constraints": [],
                        "success_outcomes": [],
                        "recommendation": "Ship in one sprint",
                        "scope_in": ["Retry flow"],
                        "scope_out": ["Payments provider migration"],
                        "risks": ["Missing telemetry"],
                        "open_questions": ["Fallback copy approval"],
                        "next_steps": ["Clarify the intended rollout audience."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime",
                side_effect=AssertionError("Incomplete PM interview should not seed Jira"),
            ) as seed_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm shape a rollout narrative for TP-20",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertIn("Examples:", command_response.message)
        self.assertTrue(command_response.data["pm_mode"])
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("product_brief_markdown", command_response.data)
        self.assertNotIn("parent_issue_key", command_response.data)
        self.assertFalse(command_response.data["ready_to_write"])
        seed_mock.assert_not_called()
        self.assertEqual(plan_mock.call_args.kwargs["request_text"], "shape a rollout narrative for TP-20")

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("pm "))
            self.assertIn("Examples:", str(latest_entry.get("answer") or ""))

    def test_pm_command_creates_parent_issue_and_returns_parent_metadata(self) -> None:
        planning_result = SimpleNamespace(
            planning_state="planning_completed",
            required_tasks=("Implement retry telemetry", "Add fallback UX validation"),
            findings=("Telemetry coverage must be explicit.",),
            recommendations=("Keep the first cut focused on customer-visible recovery.",),
            acceptance_impacts=("Acceptance criteria must mention fallback UX.",),
            open_behavior_questions=(),
            architecture_summary=(
                "Split telemetry and UX work.",
                "One child ticket per implementation slice.",
            ),
            architecture_diagram="flowchart TD\n  Parent[Parent brief] --> Child[Engineering child]",
            stages=(
                SimpleNamespace(
                    planning_state="engineering_planning",
                    persona_id="architect",
                    to_payload=lambda: {
                        "findings": ["Split telemetry and UX work."],
                        "recommendations": ["One child ticket per implementation slice."],
                        "required_tasks": ["Implement retry telemetry"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Telemetry needs explicit coverage."],
                        "mermaid_diagram": "flowchart TD\n  Parent[Parent brief] --> Child[Engineering child]",
                    },
                ),
                SimpleNamespace(
                    planning_state="security_planning",
                    persona_id="security",
                    to_payload=lambda: {
                        "findings": ["Protect retry events from abuse."],
                        "recommendations": ["Add misuse checks."],
                        "required_tasks": ["Add fallback UX validation"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Security validation is required."],
                    },
                ),
                SimpleNamespace(
                    planning_state="test_planning",
                    persona_id="qa",
                    to_payload=lambda: {
                        "findings": ["Regression coverage is required."],
                        "recommendations": ["Automate the failure-recovery path."],
                        "required_tasks": [],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Tests should cover visible recovery."],
                    },
                ),
            ),
        )
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": "The PM brief is complete and ready for parent creation.",
                    "brief": {
                        "objective": "Ship checkout recovery",
                        "user_value": "Customers recover cleanly from checkout failures.",
                        "target_user": "Customers experiencing checkout failure",
                        "primary_journey": "Retry after a failed checkout",
                        "acceptance_criteria": [
                            "Customers can retry checkout from the failure state",
                            "Fallback UX explains what to do next",
                        ],
                        "ui_references": ["Checkout failure screen"],
                        "constraints": ["Use the existing checkout system"],
                        "success_outcomes": ["Higher recovery rate from checkout failures"],
                        "recommendation": "Focus on the customer-visible fallback first.",
                        "scope_in": ["Retry telemetry", "Fallback UX"],
                        "scope_out": ["Provider migration"],
                        "risks": ["Analytics gap"],
                        "open_questions": [],
                        "next_steps": ["Review the parent feature with product"],
                    },
                    "status": "ready_to_write",
                    "ready_to_write": True,
                },
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime",
                return_value=(
                    "PM parent issue upsert complete. Created 1: TP-501. Updated 0: none.",
                    {
                        "created_parent_issue_keys": ["TP-501"],
                        "updated_parent_issue_keys": [],
                        "created_parent_issue_links": ["https://example.atlassian.net/browse/TP-501"],
                        "updated_parent_issue_links": [],
                        "all_parent_issue_keys": ["TP-501"],
                    },
                ),
            ) as seed_mock,
            patch(
                "orchestrator.api.discord.commands.ask.run_specialist_planning_fanout",
                return_value=planning_result,
            ) as planning_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_issues_with_runtime",
                return_value=(
                    "Issue upsert complete. Parent: TP-501. Created 2: TP-502, TP-503.",
                    {
                        "parent_issue_key": "TP-501",
                        "created_children": ["TP-502", "TP-503"],
                        "updated_children": [],
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_children_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm final handoff for TP-20 checkout reliability",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertEqual(command_response.data["parent_issue_key"], "TP-501")
        self.assertEqual(command_response.data["created_parent_issue_keys"], ["TP-501"])
        self.assertEqual(command_response.data["created_children"], ["TP-502", "TP-503"])
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertTrue(command_response.data["ready_to_write"])
        self.assertIn("## Approved Product Brief", str(command_response.data["product_brief_markdown"]))
        self.assertIn("PM parent issue upsert complete", command_response.message)
        self.assertIn("Issue upsert complete", command_response.message)
        self.assertEqual(plan_mock.call_args.kwargs["request_text"], "final handoff for TP-20 checkout reliability")
        seed_mock.assert_called_once()
        planning_mock.assert_called_once()
        seed_children_mock.assert_called_once()

    def test_pm_ready_to_write_stage_spi_env_blocks_parent_seed(self) -> None:
        prev_spi = os.environ.get("ORCHESTRATOR_STAGE_SPI_ENABLED")
        os.environ["ORCHESTRATOR_STAGE_SPI_ENABLED"] = "true"
        get_settings.cache_clear()
        try:
            with (
                self.session_factory() as session,
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                    return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
                ),
                patch(
                    "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                    return_value={
                        "message": "The PM brief is complete and ready for parent creation.",
                        "brief": {
                            "objective": "Ship checkout recovery",
                            "user_value": "Customers recover cleanly from checkout failures.",
                            "target_user": "Customers experiencing checkout failure",
                            "primary_journey": "Retry after a failed checkout",
                            "acceptance_criteria": [
                                "Customers can retry checkout from the failure state",
                                "Fallback UX explains what to do next",
                            ],
                            "ui_references": ["Checkout failure screen"],
                            "constraints": ["Use the existing checkout system"],
                            "success_outcomes": ["Higher recovery rate from checkout failures"],
                            "recommendation": "Focus on the customer-visible fallback first.",
                            "scope_in": ["Retry telemetry", "Fallback UX"],
                            "scope_out": ["Provider migration"],
                            "risks": ["Analytics gap"],
                            "open_questions": [],
                            "next_steps": ["Review the parent feature with product"],
                        },
                        "status": "ready_to_write",
                        "ready_to_write": True,
                    },
                ),
                patch("orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_runtime") as seed_mock,
            ):
                command_response = execute_discord_command(
                    tenant_id=self.tenant_id,
                    payload=DiscordCommandRequest(
                        user_id="u-viewer",
                        channel_id="discord-channel-1",
                        command="!pm final handoff for TP-20 checkout reliability",
                    ),
                    session=session,
                )

            self.assertTrue(command_response.ok)
            self.assertEqual(command_response.command, "pm")
            self.assertEqual(command_response.data.get("stage_plugin"), "design")
            self.assertFalse(command_response.data["stage_ready_for_implementation"])
            self.assertIn("design direction", command_response.message.lower())
            seed_mock.assert_not_called()
        finally:
            if prev_spi is None:
                os.environ.pop("ORCHESTRATOR_STAGE_SPI_ENABLED", None)
            else:
                os.environ["ORCHESTRATOR_STAGE_SPI_ENABLED"] = prev_spi
            get_settings.cache_clear()

    def test_pm_approve_is_rejected(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!pm approve rollout to beta"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !pm <product request>", response.json()["detail"])

    def test_pm_room_mode_routes_to_persona_room_runtime(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What kind of PM outcome do you need here?\n"
                        "Examples: customer-facing feature brief, internal product spec, or rollout plan."
                    ),
                    "brief": {
                        "objective": "Shape the MVP for transcription and routing.",
                        "user_value": "Stakeholders can align on the first release.",
                        "recommendation": "Clarify the intended PM artifact first.",
                        "scope_in": ["Transcription", "Routing"],
                        "scope_out": ["Full architecture design"],
                        "risks": ["The product brief is still too broad."],
                        "open_questions": ["What decision should the PM help make next?"],
                        "next_steps": ["Continue the PM interview in the room thread."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm how should this fit together",
                    command_params={"room_mode": "true"},
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertTrue(command_response.data["room_mode"])
        self.assertEqual(command_response.data.get("room_source"), "text")
        self.assertEqual(command_response.data["persona_id"], "pm")
        self.assertEqual(command_response.data["persona_name"], "PM")
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("Examples:", command_response.message)
        plan_mock.assert_called_once()

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("room "))
            self.assertTrue(str(latest_entry.get("answer") or "").startswith("pm: "))

    def test_pm_live_voice_source_routes_to_persona_runtime_with_channel_local_history(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, [{"question": "voice earlier", "answer": "security: older reply"}]),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What product decision do you need to make about session security?\n"
                        "Examples: customer-facing requirement, scope boundary, or rollout constraint."
                    ),
                    "brief": {
                        "objective": "Clarify the product requirement for session security.",
                        "user_value": "Stakeholders understand the expected security behavior.",
                        "recommendation": "Stay product-level until the PM brief is complete.",
                        "scope_in": ["Session security requirement"],
                        "scope_out": ["Detailed security design"],
                        "risks": ["The ask mixes product and implementation concerns."],
                        "open_questions": ["Which user-visible behavior matters most?"],
                        "next_steps": ["Continue the PM interview with one focused answer."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm what should we do about session security",
                    command_params={"room_source": "live_voice"},
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertTrue(command_response.data["room_mode"])
        self.assertEqual(command_response.data.get("room_source"), "live_voice")
        self.assertEqual(command_response.data["persona_id"], "pm")
        self.assertEqual(command_response.data["persona_name"], "PM")
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("Examples:", command_response.message)
        plan_mock.assert_called_once()
