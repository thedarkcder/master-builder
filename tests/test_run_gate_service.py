from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.decision_state_machine import resolve_run_gate_block
from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_types import ExecutionGateReason, ExecutionGateResolution, ExecutionGateState
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult


def _decision_result(*, outcome: str) -> DecisionEngineResult:
    if outcome == "ready_for_agent":
        gate_state = ExecutionGateState.ALLOW_EXECUTION
    elif outcome == "missing_ready_label":
        gate_state = ExecutionGateState.BLOCK_READY_LABEL
    elif outcome in {"decision_gate_required", "gtd_required", "execution_blocked"}:
        gate_state = ExecutionGateState.BLOCK_DECISION
    else:
        gate_state = ExecutionGateState.POLICY_ERROR
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
                    valid=outcome not in {"gtd_required", "execution_blocked"},
                    missing_criteria=("how_to_test",) if outcome in {"gtd_required", "execution_blocked"} else (),
                    clarification_questions=("How do we test this?",) if outcome in {"gtd_required", "execution_blocked"} else (),
                ),
            ),
            block_reason=outcome if outcome in {"decision_gate_required", "gtd_required", "execution_blocked", "missing_ready_label"} else None,
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
        execution_gate=ExecutionGateResolution(
            state=gate_state,
            reason=ExecutionGateReason(
                reason_code=outcome,
                guidance="Guidance",
                detail="Need a decision" if outcome == "decision_gate_required" else None,
                ready_label="agent:ready" if outcome == "missing_ready_label" else None,
            ) if gate_state != ExecutionGateState.ALLOW_EXECUTION else None,
        ),
    )


def test_resolve_run_gate_block_returns_decision_gate_details() -> None:
    block = resolve_run_gate_block(decision_result=_decision_result(outcome="decision_gate_required"))

    assert block is not None
    assert block.reason == "decision_gate_required"
    assert block.detail == "Need a decision"


def test_resolve_run_gate_block_returns_gtd_details() -> None:
    block = resolve_run_gate_block(decision_result=_decision_result(outcome="gtd_required"))

    assert block is not None
    assert block.reason == "gtd_required"
    assert block.detail is None


def test_resolve_run_gate_block_returns_execution_blocked_details() -> None:
    block = resolve_run_gate_block(decision_result=_decision_result(outcome="execution_blocked"))

    assert block is not None
    assert block.reason == "execution_blocked"
    assert block.detail is None


def test_resolve_run_gate_block_returns_none_when_precheck_is_clear() -> None:
    block = resolve_run_gate_block(decision_result=_decision_result(outcome="ready_for_agent"))

    assert block is None
