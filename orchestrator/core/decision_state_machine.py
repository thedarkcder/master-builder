from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_types import (
    DecisionClassification,
    DecisionEngineResult,
    ExecutionGateReason,
    ExecutionGateState,
    PrecheckOutcome,
)


class DecisionStateTransition(str, Enum):
    OPEN_CYCLE_BLOCKED = "open_cycle_blocked"
    OPEN_CYCLE_CLEAR_AND_CLOSE = "open_cycle_clear_and_close"
    TERMINAL_GATE_CLOSED_CLEAR = "terminal_gate_closed_clear"
    REUSE_CLEAR_FINGERPRINT = "reuse_clear_fingerprint"
    EVALUATE_FRESH = "evaluate_fresh"


@dataclass(frozen=True)
class DecisionStateReducerInput:
    has_case: bool
    has_open_cycle: bool
    unresolved_question_count: int
    decision_gate_closed_permanently: bool
    case_classification: DecisionClassification
    case_issue_fingerprint: str
    current_issue_fingerprint: str


def reduce_decision_state_transition(*, input_state: DecisionStateReducerInput) -> DecisionStateTransition:
    if input_state.has_case and input_state.has_open_cycle:
        if input_state.unresolved_question_count > 0:
            return DecisionStateTransition.OPEN_CYCLE_BLOCKED
        return DecisionStateTransition.OPEN_CYCLE_CLEAR_AND_CLOSE

    if input_state.has_case and input_state.decision_gate_closed_permanently:
        return DecisionStateTransition.TERMINAL_GATE_CLOSED_CLEAR

    if (
        input_state.has_case
        and not input_state.has_open_cycle
        and input_state.case_classification is DecisionClassification.CLEAR
        and input_state.case_issue_fingerprint == input_state.current_issue_fingerprint
    ):
        return DecisionStateTransition.REUSE_CLEAR_FINGERPRINT

    return DecisionStateTransition.EVALUATE_FRESH


class ExecutionAdmissionReason(str, Enum):
    DECISION_GATE_REQUIRED = "decision_gate_required"
    GTD_REQUIRED = "gtd_required"
    MISSING_READY_LABEL = "missing_ready_label"
    EXECUTION_BLOCKED = "execution_blocked"
    POLICY_EVAL_FAILED = "policy_eval_failed"
    NO_RETRYABLE_RUN = "no_retryable_run"
    RUN_ALREADY_ACTIVE = "run_already_active"
    DUPLICATE_RUN = "duplicate_run"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    TENANT_CONCURRENCY_LIMIT_REACHED = "tenant_concurrency_limit_reached"
    PR_REMEDIATION_ATTEMPT_LIMIT_REACHED = "pr_remediation_attempt_limit_reached"
    PROJECT_NOT_MAPPED = "project_not_mapped"
    READY_FOR_AGENT_BACKLOG = "ready_for_agent_backlog"
    ISSUE_IN_BACKLOG = "issue_in_backlog"
    ISSUE_NOT_ON_BOARD = "issue_not_on_board"
    BOARD_GATE_CHECK_FAILED = "board_gate_check_failed"
    BOARD_GATE_UNCONFIGURED = "board_gate_unconfigured"


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


@dataclass(frozen=True)
class RunGateBlock:
    reason: str
    guidance: str
    detail: str | None
    ready_label: str | None


def _parse_admission_reason(raw_value: str | None) -> ExecutionAdmissionReason | None:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    try:
        return ExecutionAdmissionReason(normalized)
    except ValueError:
        return None


def parse_execution_admission_reason(raw_value: object) -> ExecutionAdmissionReason | None:
    return _parse_admission_reason(str(getattr(raw_value, "value", raw_value) or ""))


def build_execution_admission_block(
    *,
    reason: ExecutionAdmissionReason,
    detail: str | None = None,
    ready_label: str | None = None,
    precheck_outcome: str | None = None,
) -> ExecutionAdmissionDecision:
    normalized_detail = str(detail or "").strip() or None
    normalized_ready_label = str(ready_label or "").strip() or None
    guidance = enqueue_reason_guidance(reason.value)
    if reason is ExecutionAdmissionReason.MISSING_READY_LABEL and normalized_ready_label:
        guidance = f"{guidance} ({normalized_ready_label})"
    return ExecutionAdmissionDecision(
        can_enqueue=False,
        precheck_outcome=precheck_outcome,
        reason=reason,
        guidance=guidance,
        detail=normalized_detail,
        ready_label=normalized_ready_label,
    )


def _resolve_gate_reason(
    *,
    gate_state: ExecutionGateState,
    gate_reason: ExecutionGateReason | None,
    fallback_reason: PrecheckOutcome | None,
) -> ExecutionGateReason:
    if gate_reason is not None:
        return gate_reason
    if gate_state == ExecutionGateState.POLICY_ERROR:
        return ExecutionGateReason(
            reason_code=PrecheckOutcome.POLICY_EVAL_FAILED.value,
            guidance=enqueue_reason_guidance(PrecheckOutcome.POLICY_EVAL_FAILED.value),
        )
    reason_code = (fallback_reason or PrecheckOutcome.DECISION_GATE_REQUIRED).value
    if gate_state == ExecutionGateState.BLOCK_READY_LABEL:
        reason_code = PrecheckOutcome.MISSING_READY_LABEL.value
    return ExecutionGateReason(
        reason_code=reason_code,
        guidance=enqueue_reason_guidance(reason_code),
    )


def resolve_run_gate_block(*, decision_result: DecisionEngineResult) -> RunGateBlock | None:
    gate_state = decision_result.execution_gate.state
    gate_reason = decision_result.execution_gate.reason
    if gate_state == ExecutionGateState.ALLOW_EXECUTION:
        return None
    resolved_reason = _resolve_gate_reason(
        gate_state=gate_state,
        gate_reason=gate_reason,
        fallback_reason=PrecheckOutcome.parse(decision_result.decision.block_reason),
    )
    return RunGateBlock(
        reason=resolved_reason.reason_code,
        guidance=resolved_reason.guidance,
        detail=resolved_reason.detail,
        ready_label=resolved_reason.ready_label,
    )


def resolve_execution_admission(*, decision_result: DecisionEngineResult) -> ExecutionAdmissionDecision:
    gate_block = resolve_run_gate_block(decision_result=decision_result)
    parsed_precheck_outcome = PrecheckOutcome.parse(getattr(decision_result.decision.pre_check, "outcome", None))
    precheck_outcome = parsed_precheck_outcome.value if parsed_precheck_outcome is not None else None
    if gate_block is None:
        return ExecutionAdmissionDecision(
            can_enqueue=True,
            precheck_outcome=precheck_outcome,
        )
    return ExecutionAdmissionDecision(
        can_enqueue=False,
        precheck_outcome=precheck_outcome,
        reason=_parse_admission_reason(gate_block.reason),
        guidance=gate_block.guidance,
        detail=gate_block.detail,
        ready_label=gate_block.ready_label,
    )
