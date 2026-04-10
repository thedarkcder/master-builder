from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.decision_types import PrecheckOutcome
from orchestrator.core.run_gate_service import resolve_run_gate_block


class ExecutionAdmissionReason(str, Enum):
    DECISION_GATE_REQUIRED = "decision_gate_required"
    GTD_REQUIRED = "gtd_required"
    MISSING_READY_LABEL = "missing_ready_label"
    EXECUTION_BLOCKED = "execution_blocked"
    POLICY_EVAL_FAILED = "policy_eval_failed"
    NO_RETRYABLE_RUN = "no_retryable_run"
    RUN_ALREADY_ACTIVE = "run_already_active"
    DUPLICATE_RUN = "duplicate_run"


@dataclass(frozen=True)
class ExecutionAdmissionDecision:
    can_enqueue: bool
    precheck_outcome: str | None
    reason: ExecutionAdmissionReason | None = None
    guidance: str | None = None
    detail: str | None = None
    ready_label: str | None = None

    @property
    def blocked(self) -> bool:
        return not self.can_enqueue

    @property
    def reason_code(self) -> str | None:
        return self.reason.value if self.reason is not None else None


def _parse_reason_code(raw_value: str | None) -> ExecutionAdmissionReason | None:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    try:
        return ExecutionAdmissionReason(normalized)
    except ValueError:
        return None


def resolve_execution_admission(*, decision_result: DecisionEngineResult) -> ExecutionAdmissionDecision:
    gate_block = resolve_run_gate_block(decision_result=decision_result)
    parsed_precheck_outcome = PrecheckOutcome.parse(
        getattr(decision_result.decision.pre_check, "outcome", None)
    )
    precheck_outcome = parsed_precheck_outcome.value if parsed_precheck_outcome is not None else None
    if gate_block is None:
        return ExecutionAdmissionDecision(
            can_enqueue=True,
            precheck_outcome=precheck_outcome,
        )
    return ExecutionAdmissionDecision(
        can_enqueue=False,
        precheck_outcome=precheck_outcome,
        reason=_parse_reason_code(gate_block.reason),
        guidance=gate_block.guidance,
        detail=gate_block.detail,
        ready_label=gate_block.ready_label,
    )
