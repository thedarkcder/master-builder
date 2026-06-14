from __future__ import annotations

import unittest

from orchestrator.core.workflow.checkpoint_codec import decode_pm_plan_payload
from orchestrator.core.workflow.checkpoint_codec import decode_dev_result_payload
from orchestrator.core.workflow.checkpoint_codec import decode_review_result_payload
from orchestrator.core.workflow.checkpoint_codec import decode_test_result_payload
from orchestrator.core.workflow.checkpoint_codec import encode_pm_plan
from orchestrator.core.workflow.checkpoint_codec import encode_stage_checkpoint_artifact
from orchestrator.core.workflow.runner import DevResult
from orchestrator.core.workflow.runner import DemoRequirement
from orchestrator.core.workflow.runner import PmPlan
from orchestrator.core.workflow.runner import QaRecording
from orchestrator.core.workflow.runner import QaResult
from orchestrator.core.workflow.runner import QaScenario
from orchestrator.core.workflow.runner import QaStep
from orchestrator.core.workflow.runner import ReviewResult
from orchestrator.core.workflow.runner import TestResult
from orchestrator.core.workflow.runner import WorkflowStageCheckpoint


class CheckpointCodecTests(unittest.TestCase):
    def test_decode_pm_plan_payload_accepts_stage_artifact_payload(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": ["risk-1"],
            "demo_requirements": [
                {
                    "title": "Show login",
                    "acceptance_criterion": "User can sign in",
                    "variants": ["Wrong password shows validation"],
                }
            ],
            "outcome": "continue",
            "next_stage": "dev",
            "execution_worker_capability": "linux",
            "resolved_prerequisites": ["repo access"],
            "unresolved_prerequisites": [],
        }

        plan = decode_pm_plan_payload(payload)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.plan_steps, ["step-1"])
        self.assertEqual(plan.acceptance_criteria, ["ac-1"])
        self.assertEqual(plan.demo_requirements[0].title, "Show login")
        self.assertEqual(plan.outcome, "continue")
        self.assertEqual(plan.next_stage, "dev")
        self.assertEqual(plan.execution_worker_capability, "linux")

    def test_decode_pm_plan_payload_rejects_invalid_outcome(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": [],
            "outcome": "approved",
            "next_stage": "dev",
            "execution_worker_capability": "linux",
        }

        self.assertIsNone(decode_pm_plan_payload(payload))

    def test_decode_pm_plan_payload_rejects_invalid_next_stage(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": [],
            "outcome": "continue",
            "next_stage": "ship-it",
            "execution_worker_capability": "linux",
        }

        self.assertIsNone(decode_pm_plan_payload(payload))

    def test_decode_pm_plan_payload_rejects_invalid_capability(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": [],
            "outcome": "continue",
            "next_stage": "dev",
            "execution_worker_capability": "unknown-capability",
        }

        self.assertIsNone(decode_pm_plan_payload(payload))

    def test_decode_pm_plan_payload_accepts_ios_demo_requirements_without_forcing_execution_worker(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": [],
            "demo_requirements": [
                {
                    "title": "Record iOS proof",
                    "acceptance_criterion": "Native flow works",
                    "capture_target": "ios",
                    "variants": [],
                }
            ],
            "outcome": "continue",
            "next_stage": "dev",
            "execution_worker_capability": "linux",
            "resolved_prerequisites": [],
            "unresolved_prerequisites": [],
        }

        plan = decode_pm_plan_payload(payload)

        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.execution_worker_capability, "linux")
        self.assertEqual(plan.demo_requirements[0].capture_target, "ios")

    def test_decode_pm_plan_payload_rejects_empty_required_lists(self) -> None:
        payload = {
            "plan_steps": [],
            "acceptance_criteria": ["ac-1"],
            "risks": [],
            "outcome": "continue",
            "next_stage": "dev",
            "execution_worker_capability": "linux",
        }

        self.assertIsNone(decode_pm_plan_payload(payload))

    def test_decode_pm_plan_payload_rejects_blocked_without_blocker_message(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": [],
            "outcome": "blocked",
            "next_stage": "dev",
            "execution_worker_capability": "linux",
        }

        self.assertIsNone(decode_pm_plan_payload(payload))

    def test_encode_pm_plan_rejects_non_canonical_requeue_target(self) -> None:
        plan = PmPlan(
            plan_steps=["step-1"],
            acceptance_criteria=["ac-1"],
            risks=[],
            demo_requirements=[DemoRequirement(title="Login flow", acceptance_criterion="User signs in")],
            outcome="requeue",
            next_stage="dev",
            execution_worker_capability="linux",
            requeue_target="MACOS",
            requeue_reason="macOS worker required for iOS signing",
        )

        with self.assertRaises(ValueError):
            encode_pm_plan(plan)

    def test_decode_pm_plan_payload_rejects_requeue_fields_for_non_requeue_outcome(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": [],
            "outcome": "continue",
            "next_stage": "dev",
            "execution_worker_capability": "linux",
            "requeue_target": "macos",
            "requeue_reason": "not needed",
        }

        self.assertIsNone(decode_pm_plan_payload(payload))

    def test_encode_pm_plan_rejects_empty_required_lists(self) -> None:
        plan = PmPlan(
            plan_steps=[],
            acceptance_criteria=["ac-1"],
            risks=[],
            outcome="continue",
            next_stage="dev",
            execution_worker_capability="linux",
        )

        with self.assertRaises(ValueError):
            encode_pm_plan(plan)

    def test_encode_pm_plan_accepts_ios_demo_requirements_without_forcing_execution_worker(self) -> None:
        plan = PmPlan(
            plan_steps=["step-1"],
            acceptance_criteria=["ac-1"],
            risks=[],
            demo_requirements=[
                DemoRequirement(
                    title="Record iOS proof",
                    acceptance_criterion="Native flow works",
                    capture_target="ios",
                )
            ],
            outcome="continue",
            next_stage="dev",
            execution_worker_capability="linux",
        )

        payload = encode_pm_plan(plan)

        self.assertEqual(payload["execution_worker_capability"], "linux")
        self.assertEqual(payload["demo_requirements"][0]["capture_target"], "ios")

    def test_decode_stage_result_payloads_reject_blocked_without_blocker_message(self) -> None:
        self.assertIsNone(
            decode_dev_result_payload(
                {
                    "change_summary": ["implemented"],
                    "outcome": "blocked",
                    "pr_url": None,
                }
            )
        )
        self.assertIsNone(
            decode_test_result_payload(
                {
                    "guidance": ["pytest -q"],
                    "validation_scope": "targeted_only",
                    "outcome": "blocked",
                    "feedback": "env missing",
                }
            )
        )
        self.assertIsNone(
            decode_review_result_payload(
                {
                    "summary": ["waiting"],
                    "outcome": "blocked",
                    "feedback": "approval missing",
                }
            )
        )

    def test_encode_stage_checkpoint_artifact_serializes_stage_specific_payloads(self) -> None:
        pm_artifact = encode_stage_checkpoint_artifact(
            WorkflowStageCheckpoint(
                stage="pm",
                attempt=1,
                status="completed",
                summary="pm",
                plan=PmPlan(
                    plan_steps=["s1"],
                    acceptance_criteria=["ac1"],
                    risks=[],
                    demo_requirements=[DemoRequirement(title="Demo", acceptance_criterion="Show feature")],
                ),
            )
        )
        dev_artifact = encode_stage_checkpoint_artifact(
            WorkflowStageCheckpoint(
                stage="dev",
                attempt=1,
                status="completed",
                summary="dev",
                dev_result=DevResult(change_summary=["c1"], pr_url="https://example/p/1"),
            )
        )
        test_artifact = encode_stage_checkpoint_artifact(
            WorkflowStageCheckpoint(
                stage="test",
                attempt=1,
                status="completed",
                summary="test",
                test_result=TestResult(guidance=["pytest -q"], validation_scope="targeted_only", feedback=None),
            )
        )
        review_artifact = encode_stage_checkpoint_artifact(
            WorkflowStageCheckpoint(
                stage="review",
                attempt=1,
                status="completed",
                summary="review",
                review_result=ReviewResult(summary=["ok"], feedback=None, pr_url="https://example/p/1"),
            )
        )
        qa_artifact = encode_stage_checkpoint_artifact(
            WorkflowStageCheckpoint(
                stage="qa",
                attempt=1,
                status="completed",
                summary="qa",
                qa_result=QaResult(
                    summary=["Recorded demos"],
                    scenarios=[
                        QaScenario(
                            name="Happy path",
                            objective="Show feature works",
                            expected_outcomes=["Feature is visible"],
                            steps=[
                                QaStep(action="goto", value="/"),
                                QaStep(action="assert_visible", selector="text=Feature"),
                            ],
                        )
                    ],
                    recordings=[
                        QaRecording(
                            name="Happy path",
                            artifact_url="https://demo.example/happy.webm",
                            object_key="demo/happy.webm",
                            capture_reference="https://preview.example",
                        )
                    ],
                ),
            )
        )

        self.assertEqual(pm_artifact["outcome"], "continue")
        self.assertEqual(pm_artifact["demo_requirements"][0]["title"], "Demo")
        self.assertEqual(dev_artifact["change_summary"], ["c1"])
        self.assertEqual(test_artifact["guidance"], ["pytest -q"])
        self.assertEqual(review_artifact["summary"], ["ok"])
        self.assertEqual(qa_artifact["recordings"][0]["object_key"], "demo/happy.webm")

    def test_encode_stage_checkpoint_artifact_allows_blocked_qa_without_scenarios(self) -> None:
        qa_artifact = encode_stage_checkpoint_artifact(
            WorkflowStageCheckpoint(
                stage="qa",
                attempt=1,
                status="blocked",
                summary="qa blocked",
                qa_result=QaResult(
                    summary=["Preview lacks an executable demo path"],
                    scenarios=[],
                    recordings=[],
                    outcome="blocked",
                    blocker_message="Preview lacks an executable demo path",
                ),
            )
        )

        self.assertEqual(qa_artifact["outcome"], "blocked")
        self.assertEqual(qa_artifact["scenarios"], [])
        self.assertEqual(qa_artifact["recordings"], [])


if __name__ == "__main__":
    unittest.main()
