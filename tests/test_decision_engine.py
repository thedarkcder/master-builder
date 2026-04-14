from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from orchestrator.core.decision_engine import (
    DecisionLabelAction,
    evaluate_ingress_precheck,
)
from orchestrator.core.decision_precheck_mapping import derive_label_actions
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_state_machine import resolve_execution_gate_state
from orchestrator.core.decision_types import ExecutionGateResolution, ExecutionGateState
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.label_action_service import apply_issue_label_actions
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.runs import resolve_enqueue_precheck_outcome
from orchestrator.core.worker.readiness import evaluate_worker_decision
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot


def _precheck(
    *,
    outcome: str = "ready_for_agent",
    ready_label_present: bool = True,
    required_worker_label_present: bool = True,
) -> PreRunCheckResult:
    return PreRunCheckResult(
        outcome=outcome,
        ready_label="agent:ready",
        ready_label_present=ready_label_present,
        required_worker_capability="linux",
        required_worker_label="worker:linux",
        required_worker_label_present=required_worker_label_present,
        decision_gate=DecisionGateResult(
            triggered=(outcome == "decision_gate_required"),
            reason="decision gate",
            missing_sections=(),
            questions=(),
            recommendation="Proceed",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=(outcome not in {"gtd_required", "execution_blocked"}),
            missing_criteria=(),
            clarification_questions=(),
        ),
    )


def _run_plan(
    *,
    pre_check_outcome: str | None = None,
    trigger_context: dict | None = None,
) -> dict:
    snapshot = ExecutionSnapshot.empty(trigger_context=trigger_context)
    if pre_check_outcome:
        snapshot.context.execution_context["pre_check_outcome"] = pre_check_outcome
    return snapshot.dump()


class DecisionEngineTests(unittest.TestCase):
    def test_ingress_precheck_returns_policy_error_when_precheck_raises(self) -> None:
        decision = evaluate_ingress_precheck(
            source="jira_webhook",
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            issue_summary="summary",
            issue_description="desc",
            issue_labels=[],
            ready_label="agent:ready",
            evaluate_pre_run_check_fn=lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        self.assertEqual(decision.block_reason, "policy_eval_failed")
        self.assertIsNone(decision.pre_check)
        self.assertIn("boom", str(decision.policy_error))

    def test_ingress_precheck_derives_label_actions(self) -> None:
        decision = evaluate_ingress_precheck(
            source="discord_run",
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            issue_summary="summary",
            issue_description="desc",
            issue_labels=[],
            ready_label="agent:ready",
            evaluate_pre_run_check_fn=lambda **_: _precheck(
                outcome="missing_ready_label",
                ready_label_present=False,
                required_worker_label_present=False,
            ),
        )
        self.assertEqual(decision.block_reason, "missing_ready_label")
        action_labels = {action.label for action in decision.label_actions}
        self.assertEqual(action_labels, {"agent:ready", "worker:linux"})

        updated = decision.with_applied_labels(["agent:ready"])
        self.assertEqual(updated.pre_check.outcome, "ready_for_agent")
        self.assertIsNone(updated.block_reason)

    def test_derive_label_actions_does_not_add_ready_label_when_gtd_is_blocked(self) -> None:
        actions = derive_label_actions(
            _precheck(
                outcome="gtd_required",
                ready_label_present=False,
                required_worker_label_present=False,
            )
        )

        self.assertEqual(
            actions,
            (
                DecisionLabelAction(
                    label="worker:linux",
                    action="add",
                    reason="required_worker_label_missing",
                ),
            ),
        )

    def test_resolve_enqueue_precheck_outcome_requires_persisted_outcome_without_source_override(self) -> None:
        self.assertIsNone(resolve_enqueue_precheck_outcome(source="admin_rerun"))
        self.assertIsNone(resolve_enqueue_precheck_outcome(source="github_pr_remediation"))
        self.assertIsNone(resolve_enqueue_precheck_outcome(source="jira_webhook"))

    def test_resolve_enqueue_precheck_outcome_forces_ready_for_remediation_trigger_context(self) -> None:
        self.assertEqual(
            resolve_enqueue_precheck_outcome(
                source="admin_rerun",
                precheck_source_plan=_run_plan(
                    pre_check_outcome="gtd_required",
                    trigger_context={"source": "github_pr_review_feedback"},
                ),
            ),
            "ready_for_agent",
        )

    def test_resolve_enqueue_precheck_outcome_does_not_force_ready_without_trigger_context(self) -> None:
        self.assertEqual(
            resolve_enqueue_precheck_outcome(
                source="admin_rerun",
                precheck_source_plan=_run_plan(pre_check_outcome="gtd_required"),
            ),
            "gtd_required",
        )

    def test_resolve_execution_gate_state_returns_structured_resolution(self) -> None:
        decision = SimpleNamespace(
            pre_check=_precheck(outcome="missing_ready_label", ready_label_present=False),
            block_reason="missing_ready_label",
            guidance="Issue is missing the configured ready label.",
            policy_error=None,
        )

        resolution = resolve_execution_gate_state(decision=decision, classification="clear")

        self.assertIsInstance(resolution, ExecutionGateResolution)
        self.assertEqual(resolution.state, ExecutionGateState.BLOCK_READY_LABEL)
        self.assertIsNotNone(resolution.reason)
        self.assertEqual(resolution.reason.reason_code, "missing_ready_label")

    def test_worker_decision_short_circuits_when_run_plan_is_ready(self) -> None:
        worker_decision = evaluate_worker_decision(
            run_plan=_run_plan(pre_check_outcome="ready_for_agent"),
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="summary",
            issue_description="desc",
            evaluate_decision_gate_fn=lambda **_: (_ for _ in ()).throw(RuntimeError("should not call")),
        )
        self.assertTrue(worker_decision.allowed)
        self.assertIsNone(worker_decision.decision_gate)
        self.assertIsNone(worker_decision.configuration_error)

    def test_worker_decision_allows_pr_remediation_when_trigger_context_present(self) -> None:
        worker_decision = evaluate_worker_decision(
            run_plan=_run_plan(
                pre_check_outcome="gtd_required",
                trigger_context={"source": "github_pr_review_feedback"},
            ),
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="TP-1: PR remediation for #12",
            issue_description="Automated remediation run triggered from GitHub PR #12.",
            evaluate_decision_gate_fn=lambda **_: (_ for _ in ()).throw(RuntimeError("should not call")),
        )
        self.assertTrue(worker_decision.allowed)
        self.assertIsNone(worker_decision.decision_gate)
        self.assertIsNone(worker_decision.configuration_error)

    def test_worker_decision_blocks_without_pr_remediation_trigger_context(self) -> None:
        worker_decision = evaluate_worker_decision(
            run_plan=_run_plan(pre_check_outcome="gtd_required"),
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="TP-1: PR remediation for #12",
            issue_description=(
                "Automated remediation run triggered from GitHub PR #12 "
                "(https://github.com/org/repo/pull/12)."
            ),
            evaluate_decision_gate_fn=lambda **_: (_ for _ in ()).throw(RuntimeError("should not call")),
        )
        self.assertFalse(worker_decision.allowed)
        self.assertEqual(worker_decision.block_reason, "policy_eval_failed")
        self.assertIsNone(worker_decision.decision_gate)
        self.assertIn("non-ready pre_check_outcome", str(worker_decision.configuration_error or ""))

    def test_worker_decision_returns_configuration_error_when_snapshot_missing(self) -> None:
        worker_decision = evaluate_worker_decision(
            run_plan=None,
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="summary",
            issue_description="desc",
        )
        self.assertFalse(worker_decision.allowed)
        self.assertIsNone(worker_decision.decision_gate)
        self.assertIsNotNone(worker_decision.configuration_error)
        self.assertEqual(worker_decision.block_reason, "policy_eval_failed")

    def test_worker_decision_fails_closed_when_persisted_precheck_outcome_missing(self) -> None:
        worker_decision = evaluate_worker_decision(
            run_plan=_run_plan(),
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="summary",
            issue_description="desc",
        )
        self.assertFalse(worker_decision.allowed)
        self.assertIsNone(worker_decision.decision_gate)
        self.assertEqual(worker_decision.block_reason, "policy_eval_failed")
        self.assertIn("missing persisted pre_check_outcome", str(worker_decision.configuration_error or ""))

    def test_worker_decision_uses_persisted_precheck_outcome_ready(self) -> None:
        worker_decision = evaluate_worker_decision(
            run_plan=_run_plan(pre_check_outcome="ready_for_agent"),
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="summary",
            issue_description="desc",
        )
        self.assertTrue(worker_decision.allowed)
        self.assertIsNone(worker_decision.classification)
        self.assertIsNone(worker_decision.decision_gate)
        self.assertIsNone(worker_decision.block_reason)

    def test_worker_decision_blocks_on_missing_ready_label_from_persisted_outcome(self) -> None:
        plan = _run_plan(pre_check_outcome="missing_ready_label")
        snapshot = ExecutionSnapshot.load(plan)
        assert snapshot is not None
        snapshot.context.execution_context["run_not_ready"] = {"ready_label": "agent:ready"}
        worker_decision = evaluate_worker_decision(
            run_plan=snapshot.dump(),
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="summary",
            issue_description="desc",
            tenant=SimpleNamespace(tenant_id="t1", jira_config={"ready_label": "agent:ready"}),
        )

        self.assertFalse(worker_decision.allowed)
        self.assertEqual(worker_decision.block_reason, "policy_eval_failed")
        self.assertIsNone(worker_decision.decision_gate)
        self.assertIn("non-ready pre_check_outcome", str(worker_decision.configuration_error or ""))

    def test_worker_decision_blocks_on_gtd_required_from_persisted_outcome(self) -> None:
        plan = _run_plan(pre_check_outcome="gtd_required")
        snapshot = ExecutionSnapshot.load(plan)
        assert snapshot is not None
        snapshot.context.execution_context["run_not_ready"] = {"reason": "Dependencies unresolved"}
        worker_decision = evaluate_worker_decision(
            run_plan=snapshot.dump(),
            tenant_id="t1",
            project_id="p1",
            issue_key="TP-1",
            run_id="run-1",
            issue_summary="summary",
            issue_description="desc",
        )
        self.assertFalse(worker_decision.allowed)
        self.assertIsNone(worker_decision.decision_gate)
        self.assertEqual(worker_decision.block_reason, "policy_eval_failed")
        self.assertEqual(worker_decision.classification, "clear")
        self.assertIn("non-ready pre_check_outcome", str(worker_decision.configuration_error or ""))

class LabelActionServiceTests(unittest.TestCase):
    def test_apply_issue_label_actions_respects_policy_and_dedupes(self) -> None:
        oauth_client = SimpleNamespace(add_issue_labels=mock.MagicMock())
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        tenant = SimpleNamespace(
            tenant_id="tenant-1",
            policy_config={"allow_label_mutations": True},
        )
        actions = (
            DecisionLabelAction(label="agent:ready", action="add", reason="ready_label_missing"),
            DecisionLabelAction(label="worker:linux", action="add", reason="required_worker_label_missing"),
            DecisionLabelAction(label="worker:linux", action="add", reason="required_worker_label_missing"),
        )
        result = apply_issue_label_actions(
            session=SimpleNamespace(),
            tenant=tenant,
            project_policy_overrides={},
            issue_key="TP-1",
            existing_labels=["worker:linux"],
            actions=actions,
            settings=SimpleNamespace(),
            tenant_jira_oauth_context_fn=lambda **_: oauth_context,
            logger=SimpleNamespace(warning=lambda *_, **__: None),
        )
        self.assertEqual(result.applied_labels, ("agent:ready",))
        oauth_client.add_issue_labels.assert_called_once_with(
            access_token="tok-1",
            cloud_id="cloud-1",
            issue_id_or_key="TP-1",
            labels=["agent:ready"],
        )

    def test_apply_issue_label_actions_skips_when_mutations_disabled(self) -> None:
        result = apply_issue_label_actions(
            session=SimpleNamespace(),
            tenant=SimpleNamespace(
                tenant_id="tenant-1",
                policy_config={"allow_label_mutations": False},
            ),
            project_policy_overrides={},
            issue_key="TP-1",
            existing_labels=[],
            actions=(
                DecisionLabelAction(label="agent:ready", action="add", reason="ready_label_missing"),
            ),
            settings=SimpleNamespace(),
            tenant_jira_oauth_context_fn=lambda **_: None,
            logger=SimpleNamespace(warning=lambda *_, **__: None),
        )
        self.assertEqual(result.applied_labels, ())
        self.assertEqual(result.skipped_reason, "label_mutations_disabled")
