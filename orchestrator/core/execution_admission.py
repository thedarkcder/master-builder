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

    def discord_conflict_detail(self) -> str:
        detail = str(self.guidance or "").strip()
        if self.reason_code == "missing_ready_label" and self.ready_label:
            return f"{detail} ({self.ready_label})"
        return detail

    def jira_response_fields(self, *, fallback_ready_label: str | None = None) -> dict[str, str | None]:
        if self.can_enqueue:
            return {}
        response_fields: dict[str, str | None] = {
            "reason": self.reason_code,
            "guidance": self.guidance,
        }
        if self.reason_code == "decision_gate_required":
            response_fields["decision_gate_reason"] = self.detail
        if self.reason_code == "missing_ready_label":
            response_fields["ready_label"] = self.ready_label or fallback_ready_label
        return response_fields

    def notification_extra_detail(self) -> str | None:
        if self.reason_code == "decision_gate_required" and self.detail:
            return f"decision_gate_reason={self.detail}"
        return self.detail


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
