from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.core.worker.process_service import _emit_detailed_jira_feedback
from orchestrator.core.worker.process_service import _extract_capability_requeue_target
from orchestrator.core.workflow.runner import PmPlan, WorkflowDiagnostics, WorkflowResult


class WorkerProcessServiceTests(unittest.TestCase):
    def test_emit_detailed_jira_feedback_sends_dev_and_review_comments(self) -> None:
        send_jira = MagicMock()
        workflow_result = WorkflowResult(
            succeeded=False,
            plan=None,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            dev_rationale=["Implemented onboarding flow", "Added splash navigation guard"],
            review_summary=["Needs state-machine transition fix"],
            review_feedback="Please remove timer-based progression.",
        )
        _emit_detailed_jira_feedback(
            session=MagicMock(),
            tenant=SimpleNamespace(),
            run=SimpleNamespace(run_id="run-1", issue_key="GP-1"),
            settings=SimpleNamespace(),
            workflow_result=workflow_result,
            send_jira_message_fn=send_jira,
        )

        stages = [call.kwargs["stage"] for call in send_jira.call_args_list]
        self.assertEqual(stages, ["dev_rationale", "review_summary", "review_feedback"])

    def test_extract_capability_requeue_target_from_plan(self) -> None:
        workflow_result = WorkflowResult(
            succeeded=False,
            plan=PmPlan(
                plan_steps=["Plan"],
                acceptance_criteria=["AC"],
                risks=[],
                execution_worker_capability="macos",
            ),
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            diagnostics=WorkflowDiagnostics(
                stage="pm",
                message="Execution capability mismatch: PM selected macos but current worker is linux.",
                attempts=1,
                history=[],
            ),
        )
        self.assertEqual(_extract_capability_requeue_target(workflow_result), "macos")

    def test_extract_capability_requeue_target_returns_none_for_non_mismatch(self) -> None:
        workflow_result = WorkflowResult(
            succeeded=False,
            plan=None,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            diagnostics=WorkflowDiagnostics(
                stage="test",
                message="Max workflow attempts reached after test failures",
                attempts=1,
                history=[],
            ),
        )
        self.assertIsNone(_extract_capability_requeue_target(workflow_result))


if __name__ == "__main__":
    unittest.main()
