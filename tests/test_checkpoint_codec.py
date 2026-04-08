from __future__ import annotations

import unittest

from orchestrator.core.workflow.checkpoint_codec import decode_pm_plan_payload
from orchestrator.core.workflow.checkpoint_codec import decode_dev_result_payload
from orchestrator.core.workflow.checkpoint_codec import decode_review_result_payload
from orchestrator.core.workflow.checkpoint_codec import decode_test_result_payload
from orchestrator.core.workflow.checkpoint_codec import encode_pm_plan
from orchestrator.core.workflow.checkpoint_codec import encode_stage_checkpoint_artifact
from orchestrator.core.workflow.runner import DevResult
from orchestrator.core.workflow.runner import PmPlan
from orchestrator.core.workflow.runner import ReviewResult
from orchestrator.core.workflow.runner import TestResult
from orchestrator.core.workflow.runner import WorkflowStageCheckpoint


class CheckpointCodecTests(unittest.TestCase):
    def test_decode_pm_plan_payload_accepts_stage_artifact_payload(self) -> None:
        payload = {
            "plan_steps": ["step-1"],
            "acceptance_criteria": ["ac-1"],
            "risks": ["risk-1"],
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
                test_result=TestResult(guidance=["pytest -q"], feedback=None),
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

        self.assertEqual(pm_artifact["outcome"], "continue")
        self.assertEqual(dev_artifact["change_summary"], ["c1"])
        self.assertEqual(test_artifact["guidance"], ["pytest -q"])
        self.assertEqual(review_artifact["summary"], ["ok"])


if __name__ == "__main__":
    unittest.main()
