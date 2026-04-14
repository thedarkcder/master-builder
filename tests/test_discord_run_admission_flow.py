from unittest.mock import patch

import pytest

from orchestrator.tools.jira_oauth import JiraIssueDetail, JiraIssuePreview
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
