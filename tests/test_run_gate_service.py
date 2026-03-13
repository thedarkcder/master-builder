from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.run_gate_service import resolve_run_gate_block


def _decision_result(*, outcome: str) -> DecisionEngineResult:
    return DecisionEngineResult(
        decision=SimpleNamespace(
            pre_check=PreRunCheckResult(
                outcome=outcome,
                ready_label="agent:ready",
                ready_label_present=(outcome != "missing_ready_label"),
                required_worker_capability="linux",
                required_worker_label="worker:linux",
                required_worker_label_present=True,
                decision_gate=DecisionGateResult(
                    triggered=outcome == "decision_gate_required",
                    reason="Need a decision",
                    missing_sections=("owner",),
                    questions=("Who owns this?",),
                    recommendation="Clarify before execution.",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=outcome != "gtd_required",
                    missing_criteria=("how_to_test",) if outcome == "gtd_required" else (),
                    clarification_questions=("How do we test this?",) if outcome == "gtd_required" else (),
                ),
            ),
            block_reason=outcome if outcome in {"decision_gate_required", "gtd_required", "missing_ready_label"} else None,
            guidance=None,
            policy_error=None,
        ),
        issue_labels=[],
        classification="clear",
        missing_slots=[],
        auto_resolved_slots=[],
        case_id="case-1",
        case_state="clear",
        cycle_id=None,
        outbox_effect_ids=(),
        duplicate_event=False,
    )


def test_resolve_run_gate_block_returns_decision_gate_details() -> None:
    block = resolve_run_gate_block(decision_result=_decision_result(outcome="decision_gate_required"))

    assert block is not None
    assert block.reason == "decision_gate_required"
    assert block.decision_gate_reason == "Need a decision"
    assert block.decision_gate_questions == ("Who owns this?",)


def test_resolve_run_gate_block_returns_gtd_details() -> None:
    block = resolve_run_gate_block(decision_result=_decision_result(outcome="gtd_required"))

    assert block is not None
    assert block.reason == "gtd_required"
    assert block.gtd_missing_criteria == ("how_to_test",)
    assert block.gtd_questions == ("How do we test this?",)


def test_resolve_run_gate_block_returns_none_when_precheck_is_clear() -> None:
    block = resolve_run_gate_block(decision_result=_decision_result(outcome="ready_for_agent"))

    assert block is None
