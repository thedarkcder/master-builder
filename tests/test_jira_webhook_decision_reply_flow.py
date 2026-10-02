from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.decision.types import DecisionClassification
from tests.test_support.jira_webhook_harness import JiraWebhookHarness


pytestmark = pytest.mark.contract


class JiraWebhookDecisionReplyFlowTests(JiraWebhookHarness):
    def test_webhook_decision_reply_uses_captured_evidence_id_for_decision_event_idempotency(
        self,
    ) -> None:
        payload = self._jira_issue_payload(issue_key="TP-906", status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-2"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Decision reply content"}],
                    }
                ],
            },
        }
        decision_result = SimpleNamespace(
            classification=DecisionClassification.DECISION_GATE,
            cycle_id="cycle-1",
            decision=SimpleNamespace(
                pre_check=SimpleNamespace(
                    decision_gate=SimpleNamespace(
                        questions=("Need entitlement confirmation",)
                    )
                )
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.active_case_and_cycle_for_issue",
                return_value=(
                    SimpleNamespace(case_id="case-1"),
                    SimpleNamespace(cycle_id="cycle-1"),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.capture_decision_reply_and_recheck",
                return_value=SimpleNamespace(
                    evidence_id="evidence-jira-1",
                    decision_result=decision_result,
                ),
            ) as capture_mock,
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.load_cycle_question_feedback",
                return_value=(
                    {
                        "question_id": "dg_1",
                        "question_text": "Need entitlement confirmation",
                    },
                ),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-906")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        decision_event = capture_mock.call_args.kwargs["decision_event_factory"](
            SimpleNamespace(evidence_id="evidence-jira-1")
        )
        self.assertEqual(
            decision_event.idempotency_key, "decision-reply:evidence-jira-1"
        )

    def test_webhook_decision_reply_failure_handles_codex_runtime_error(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-907", status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-3"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Decision reply content"}],
                    }
                ],
            },
        }
        with (
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.active_case_and_cycle_for_issue",
                return_value=(
                    SimpleNamespace(case_id="case-1"),
                    SimpleNamespace(cycle_id="cycle-1"),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.capture_decision_reply_and_recheck",
                side_effect=CodexRuntimeError("bad structured output"),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-907")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

    def test_webhook_project_not_mapped_does_not_process_decision_reply(self) -> None:
        payload = self._jira_issue_payload(issue_key="NOPE-1", status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-4"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Decision reply content"}],
                    }
                ],
            },
        }
        with (
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.stage_handle_comment_decision_reply",
            ) as reply_stage_mock,
            patch(
                "orchestrator.api.webhooks.jira_admission_flow.evaluate_precheck_decision_with_labels",
            ) as evaluate_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="NOPE-1")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        reply_stage_mock.assert_not_called()
        evaluate_mock.assert_not_called()
