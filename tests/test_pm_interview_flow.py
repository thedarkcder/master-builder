import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.schemas import DiscordCommandRequest
from tests.test_discord_commands import DiscordCommandApiTests


class PmInterviewFlowTests(DiscordCommandApiTests):
    def test_incomplete_pm_request_stays_in_interview_mode_and_does_not_seed_jira(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {}, []),
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What kind of share feature do you mean?\n"
                        "Examples: share the app link with friends, invite friends into a workspace, "
                        "or refer friends for rewards."
                    ),
                    "brief": {
                        "objective": "Help users share the product.",
                        "user_value": "Users can bring others into the product more easily.",
                        "acceptance_criteria": [],
                        "ui_references": [],
                        "recommendation": "Clarify the type of sharing before shaping the feature.",
                        "scope_in": [],
                        "scope_out": [],
                        "risks": ["Feature intent is ambiguous."],
                        "open_questions": [
                            "Is this a share link, collaborative invite, or referral program?"
                        ],
                        "next_steps": [
                            "Ask one clarification question before writing the parent feature."
                        ],
                        "success_outcomes": [],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ),
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_codex",
                new=MagicMock(side_effect=AssertionError("PM interview should not seed Jira before completion")),
            ) as seed_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm create a share feature",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "pm")
        self.assertTrue(response.data["pm_mode"])
        self.assertIn("Examples:", response.message)
        self.assertNotIn("parent_issue_key", response.data)
        seed_mock.assert_not_called()

    def test_complete_pm_request_writes_parent_and_plans_children(self) -> None:
        planning_result = SimpleNamespace(
            planning_state="planning_completed",
            required_tasks=("Implement share entry points", "Add share-link verification"),
            findings=("Share flows need abuse checks.",),
            recommendations=("Keep the first version link-only.",),
            acceptance_impacts=("Acceptance criteria should cover store fallback.",),
            open_behavior_questions=(),
            stages=(
                SimpleNamespace(
                    planning_state="engineering_planning",
                    to_payload=lambda: {
                        "findings": ["Break the work into onboarding and profile slices."],
                        "recommendations": ["Use one child ticket per implementation slice."],
                        "required_tasks": ["Implement share entry points"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Needs entry points in onboarding and profile."],
                    },
                ),
                SimpleNamespace(
                    planning_state="security_planning",
                    to_payload=lambda: {
                        "findings": ["The link flow needs abuse controls."],
                        "recommendations": ["Enforce rate limiting."],
                        "required_tasks": ["Add share-link verification"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Security checks must be covered in acceptance."],
                    },
                ),
                SimpleNamespace(
                    planning_state="test_planning",
                    to_payload=lambda: {
                        "findings": ["Regression coverage is required."],
                        "recommendations": ["Cover repeat-share misuse."],
                        "required_tasks": [],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Tests should prove the visible share outcome."],
                    },
                ),
            ),
        )
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": "The PM brief is complete and ready for parent creation.",
                    "brief": {
                        "objective": "Users can share the app with friends",
                        "user_value": "Users can invite friends without friction.",
                        "target_user": "New users",
                        "primary_journey": "From onboarding and profile",
                        "acceptance_criteria": [
                            "A user can share a link from onboarding",
                            "A user can share a link from profile",
                        ],
                        "scope_in": ["Share link", "Profile entry point", "Onboarding entry point"],
                        "scope_out": ["Referral rewards"],
                        "ui_references": ["Profile page", "Onboarding screen"],
                        "constraints": ["iOS and Android"],
                        "risks": ["Abuse and spam"],
                        "success_outcomes": ["More invites sent"],
                        "recommendation": "Ship the link-only version first.",
                        "open_questions": [],
                        "next_steps": ["Create the parent feature and planning tickets."],
                    },
                    "status": "ready_to_write",
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_codex",
                return_value=(
                    "PM parent issue upsert complete. Created 1: TP-501. Updated 0: none.",
                    {
                        "all_parent_issue_keys": ["TP-501"],
                        "created_parent_issue_keys": ["TP-501"],
                        "updated_parent_issue_keys": [],
                        "created_parent_issue_links": ["https://jira.example.com/browse/TP-501"],
                        "updated_parent_issue_links": [],
                    },
                ),
            ) as seed_parent_mock,
            patch(
                "orchestrator.api.discord.commands.ask.run_specialist_planning_fanout",
                return_value=planning_result,
            ) as planning_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_issues_with_codex",
                return_value=(
                    "Issue upsert complete. Parent: TP-501. Created 2: TP-502, TP-503.",
                    {
                        "parent_issue_key": "TP-501",
                        "created_children": ["TP-502", "TP-503"],
                        "updated_children": [],
                        "children_sync_status": "children_current",
                        "planning_state": "planning_completed",
                    },
                ),
            ) as seed_children_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm create a share feature",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "pm")
        self.assertEqual(response.data["parent_issue_key"], "TP-501")
        self.assertEqual(response.data["created_children"], ["TP-502", "TP-503"])
        self.assertEqual(response.data["planning_state"], "planning_completed")
        self.assertIn("TP-501", response.message)
        self.assertIn("Issue upsert complete", response.message)
        self.assertEqual(seed_parent_mock.call_args.kwargs["pm_status"], "ready_to_write")
        self.assertEqual(seed_children_mock.call_args.kwargs["pm_status"], "pm_completed")
        planning_package = seed_children_mock.call_args.kwargs["planning_package"]
        self.assertEqual(planning_package["planning_state"], "planning_completed")
        self.assertEqual(len(planning_package["child_issues"]), 2)
        planning_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
