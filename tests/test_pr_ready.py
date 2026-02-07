from __future__ import annotations

import unittest

from orchestrator.core.pr_ready import evaluate_pr_readiness
from orchestrator.tools.github_app import WorkflowCheckSuite


class PrReadinessTests(unittest.TestCase):
    def test_ready_when_required_workflows_green_and_review_present(self) -> None:
        result = evaluate_pr_readiness(
            review_summary_present=True,
            required_workflows=("CI", "Security"),
            workflow_checks=[
                WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
            ],
        )
        self.assertTrue(result.ready)
        self.assertEqual(result.state, "ready")

    def test_not_ready_when_review_summary_missing(self) -> None:
        result = evaluate_pr_readiness(
            review_summary_present=False,
            required_workflows=("CI", "Security"),
            workflow_checks=[
                WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
            ],
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.state, "missing_review_summary")

    def test_not_ready_when_checks_pending(self) -> None:
        result = evaluate_pr_readiness(
            review_summary_present=True,
            required_workflows=("CI", "Security"),
            workflow_checks=[
                WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                WorkflowCheckSuite(name="Security", status="in_progress", conclusion=None),
            ],
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.state, "pending_checks")
        self.assertEqual(result.pending_workflows, ("Security",))

    def test_not_ready_when_checks_failing(self) -> None:
        result = evaluate_pr_readiness(
            review_summary_present=True,
            required_workflows=("CI", "Security"),
            workflow_checks=[
                WorkflowCheckSuite(name="CI", status="completed", conclusion="failure"),
                WorkflowCheckSuite(name="Security", status="completed", conclusion="success"),
            ],
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.state, "failing_checks")
        self.assertEqual(result.failing_workflows, ("CI",))

    def test_not_ready_when_required_checks_missing(self) -> None:
        result = evaluate_pr_readiness(
            review_summary_present=True,
            required_workflows=("CI", "Security"),
            workflow_checks=[WorkflowCheckSuite(name="CI", status="completed", conclusion="success")],
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.state, "missing_checks")
        self.assertEqual(result.missing_workflows, ("Security",))
