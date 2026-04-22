from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.runs import EnqueueRunResult
from orchestrator.core.run_enqueue_types import EnqueueFailureReason
from orchestrator.tools.atlassian_oauth import JiraIssueDetail, JiraIssuePreview
from tests.test_support.discord_command_reply_harness import DiscordCommandReplyHarness


pytestmark = pytest.mark.contract


class DiscordRunAdmissionFlowTests(DiscordCommandReplyHarness):
    def test_run_rejects_when_ready_label_is_missing(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-20",
                    summary="Do thing",
                    status="To Do",
                    description="Objective: run command should carry Jira detail context.",
                    labels=[],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._missing_ready_decision_result(),
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("missing the configured ready label", response.json()["detail"])
        self.assertIn("agent:ready", response.json()["detail"])

    def test_run_surfaces_typed_enqueue_conflict_for_active_run(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-20",
                    summary="Do thing",
                    status="To Do",
                    description="Objective: run command should carry Jira detail context.",
                    labels=["agent:ready"],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._ready_decision_result(),
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.enqueue_issue_run_with_precheck",
                return_value=EnqueueRunResult(
                    enqueued=False,
                    reason=EnqueueFailureReason.RUN_ALREADY_ACTIVE,
                    run=SimpleNamespace(run_id="run-1", status="queued"),
                ),
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("run_already_active", response.json()["detail"])
        self.assertIn("run-1", response.json()["detail"])
