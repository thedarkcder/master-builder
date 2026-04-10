from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.run_gate_service import resolve_run_gate_block


@dataclass(frozen=True)
class ExecutionAdmissionDecision:
    can_enqueue: bool
    precheck_outcome: str | None
    reason_code: str | None = None
    guidance: str | None = None
    detail: str | None = None
    ready_label: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.can_enqueue


def resolve_execution_admission(*, decision_result: DecisionEngineResult) -> ExecutionAdmissionDecision:
    gate_block = resolve_run_gate_block(decision_result=decision_result)
    precheck_outcome = (
        str(getattr(decision_result.decision.pre_check, "outcome", "") or "").strip() or None
    )
    if gate_block is None:
        return ExecutionAdmissionDecision(
            can_enqueue=True,
            precheck_outcome=precheck_outcome,
        )
    return ExecutionAdmissionDecision(
        can_enqueue=False,
        precheck_outcome=precheck_outcome,
        reason_code=gate_block.reason,
        guidance=gate_block.guidance,
        detail=gate_block.detail,
        ready_label=gate_block.ready_label,
    )
