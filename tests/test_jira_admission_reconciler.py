from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.decision_types import ExecutionGateReason, ExecutionGateResolution, ExecutionGateState
from orchestrator.core.execution_admission import resolve_execution_admission


def _decision_result(*, state: ExecutionGateState, reason_code: str | None) -> object:
    reason = (
        ExecutionGateReason(
            reason_code=reason_code or "",
            guidance="Guidance",
            detail="detail" if reason_code == "decision_gate_required" else None,
            ready_label="agent:ready" if reason_code == "missing_ready_label" else None,
        )
        if state != ExecutionGateState.ALLOW_EXECUTION
        else None
    )
    return SimpleNamespace(
        decision=SimpleNamespace(
            pre_check=SimpleNamespace(outcome=reason_code or "ready_for_agent"),
            block_reason=reason_code,
        ),
        execution_gate=ExecutionGateResolution(state=state, reason=reason),
    )


def test_resolve_execution_admission_blocks_when_execution_gate_blocks() -> None:
    decision = resolve_execution_admission(
        decision_result=_decision_result(
            state=ExecutionGateState.BLOCK_DECISION,
            reason_code="decision_gate_required",
        )
    )
    assert decision.can_enqueue is False
    assert decision.reason_code == "decision_gate_required"
    assert decision.blocked is True


def test_resolve_execution_admission_allows_when_execution_gate_allows() -> None:
    decision = resolve_execution_admission(
        decision_result=_decision_result(
            state=ExecutionGateState.ALLOW_EXECUTION,
            reason_code=None,
        )
    )
    assert decision.can_enqueue is True
    assert decision.precheck_outcome == "ready_for_agent"
    assert decision.jira_response_fields() == {}


def test_resolve_execution_admission_formats_discord_missing_ready_label_detail() -> None:
    decision = resolve_execution_admission(
        decision_result=_decision_result(
            state=ExecutionGateState.BLOCK_READY_LABEL,
            reason_code="missing_ready_label",
        )
    )
    assert decision.discord_conflict_detail() == "Guidance (agent:ready)"
