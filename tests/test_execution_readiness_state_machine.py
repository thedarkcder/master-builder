from __future__ import annotations

from orchestrator.core.execution_readiness_state_machine import (
    ReadinessState,
    blocking_reason_for_outcome,
    resolve_precheck_outcome,
    resolve_readiness_decision,
)


def test_blocking_reason_for_outcome_allows_only_blocking_outcomes() -> None:
    assert blocking_reason_for_outcome("decision_gate_required") == "decision_gate_required"
    assert blocking_reason_for_outcome("missing_ready_label") == "missing_ready_label"
    assert blocking_reason_for_outcome("ready_for_agent") is None


def test_resolve_readiness_decision_prioritizes_policy_error() -> None:
    decision = resolve_readiness_decision(
        policy_error="runtime failed",
        block_reason="gtd_required",
        classification="gtd",
    )
    assert decision.state == ReadinessState.POLICY_ERROR
    assert decision.reason_code == "policy_eval_failed"


def test_resolve_readiness_decision_classification_blocks_when_reason_missing() -> None:
    decision = resolve_readiness_decision(
        policy_error=None,
        block_reason=None,
        classification="decision_gate",
    )
    assert decision.state == ReadinessState.BLOCKED_DECISION
    assert decision.reason_code == "decision_gate_required"


def test_resolve_precheck_outcome_follows_transition_order() -> None:
    assert (
        resolve_precheck_outcome(
            decision_gate_triggered=True,
            gtd_valid=False,
            ready_label_present=False,
            ready_label_required=True,
        )
        == "decision_gate_required"
    )
    assert (
        resolve_precheck_outcome(
            decision_gate_triggered=False,
            gtd_valid=False,
            ready_label_present=False,
            ready_label_required=True,
        )
        == "gtd_required"
    )
    assert (
        resolve_precheck_outcome(
            decision_gate_triggered=False,
            gtd_valid=True,
            ready_label_present=False,
            ready_label_required=True,
        )
        == "missing_ready_label"
    )
    assert (
        resolve_precheck_outcome(
            decision_gate_triggered=False,
            gtd_valid=True,
            ready_label_present=True,
            ready_label_required=True,
        )
        == "ready_for_agent"
    )
