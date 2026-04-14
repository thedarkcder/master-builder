from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from orchestrator.core.decision_snapshot_codec import (
    PrecheckSnapshot,
    apply_frozen_cycle_questions,
)
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_types import (
    DecisionClassification,
    IngressDecision,
    PrecheckOutcome,
    blocking_reason_for_precheck,
    guidance_for_precheck_block_reason,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.storage.models import DecisionCase, DecisionCycle


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


def case_state_for_decision(*, decision: IngressDecision) -> str:
    pre_check = decision.pre_check
    parsed_block_reason = PrecheckOutcome.parse(decision.block_reason)
    if parsed_block_reason is PrecheckOutcome.DECISION_GATE_REQUIRED:
        return "blocked_decision_gate"
    if parsed_block_reason in {PrecheckOutcome.GTD_REQUIRED, PrecheckOutcome.EXECUTION_BLOCKED}:
        return "blocked_gtd"
    if pre_check is not None and PrecheckOutcome.parse(getattr(pre_check, "outcome", None)) is PrecheckOutcome.READY_FOR_AGENT:
        return "ready_for_execution"
    return "clear"


def decision_reason(*, pre_check: object, classification: str) -> str | None:
    if pre_check is None:
        return None
    parsed_classification = DecisionClassification.parse(classification)
    if parsed_classification.includes_decision_gate:
        decision_gate = getattr(pre_check, "decision_gate", None)
        reason = str(getattr(decision_gate, "reason", "") or "").strip() if decision_gate is not None else ""
        if reason:
            return reason
    if parsed_classification.includes_gtd:
        missing = [
            str(item).strip()
            for item in getattr(pre_check, "gtd_missing_criteria", ())
            if str(item).strip()
        ]
        if missing:
            return "Missing GTD criteria: " + ", ".join(missing)
    return None


def is_question_driven_state(*, classification: DecisionClassification | str, block_reason: str | None) -> bool:
    parsed_classification = (
        classification if isinstance(classification, DecisionClassification) else DecisionClassification.parse(classification)
    )
    parsed_block_reason = PrecheckOutcome.parse(block_reason)
    return parsed_classification.blocks_execution and parsed_block_reason in {
        PrecheckOutcome.DECISION_GATE_REQUIRED,
        PrecheckOutcome.GTD_REQUIRED,
    }


def decision_from_snapshot(
    *,
    snapshot: dict[str, Any],
    source: str,
    classification: str,
    cycle: DecisionCycle | None,
    case: DecisionCase,
) -> IngressDecision:
    pre_check_snapshot = PrecheckSnapshot.load(snapshot.get("pre_check"))
    pre_check = pre_check_snapshot.to_precheck() if pre_check_snapshot is not None else None
    if pre_check is not None and cycle is not None and cycle.status == "open":
        pre_check = apply_frozen_cycle_questions(
            pre_check=pre_check,
            cycle_question_set=list(cycle.question_set_json),
            unresolved_question_ids=list(cycle.unresolved_question_ids_json),
            cycle_reason=cycle.reason,
            classification=DecisionClassification.parse(classification),
        )

    if DecisionClassification.parse(classification) is DecisionClassification.CLEAR:
        normalized_pre_check = pre_check
        if normalized_pre_check is None:
            ready_label = str(case.ready_label or "").strip() or None
            ready_label_present = bool(case.ready_label_present)
            outcome = (
                PrecheckOutcome.MISSING_READY_LABEL.value
                if ready_label and not ready_label_present
                else PrecheckOutcome.READY_FOR_AGENT.value
            )
            normalized_pre_check = PreRunCheckResult(
                outcome=outcome,
                ready_label=ready_label,
                ready_label_present=ready_label_present,
                required_worker_capability=str(case.required_worker_capability or "").strip(),
                required_worker_label=str(case.required_worker_label or "").strip(),
                required_worker_label_present=bool(case.required_worker_label_present),
                decision_gate=DecisionGateResult(
                    triggered=False,
                    reason="Decision Gate not required",
                    missing_sections=(),
                    questions=(),
                    recommendation="Proceed with execution.",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
            )
        if PrecheckOutcome.parse(getattr(normalized_pre_check, "outcome", None)) is PrecheckOutcome.DECISION_GATE_REQUIRED:
            normalized_pre_check = PreRunCheckResult(
                outcome=PrecheckOutcome.READY_FOR_AGENT.value,
                ready_label=normalized_pre_check.ready_label,
                ready_label_present=normalized_pre_check.ready_label_present,
                required_worker_capability=normalized_pre_check.required_worker_capability,
                required_worker_label=normalized_pre_check.required_worker_label,
                required_worker_label_present=normalized_pre_check.required_worker_label_present,
                decision_gate=normalized_pre_check.decision_gate,
                gtd=normalized_pre_check.gtd,
            )
        normalized_block_reason = blocking_reason_for_precheck(normalized_pre_check)
        return IngressDecision(
            source=source,  # type: ignore[arg-type]
            pre_check=normalized_pre_check,
            block_reason=normalized_block_reason,
            guidance=guidance_for_precheck_block_reason(
                block_reason=normalized_block_reason,
                ready_label=str(getattr(normalized_pre_check, "ready_label", "") or "").strip() or None,
            ),
            policy_error=None,
            label_actions=(),
        )

    return IngressDecision(
        source=source,  # type: ignore[arg-type]
        pre_check=pre_check,
        block_reason=str(snapshot.get("block_reason") or case.blocked_reason or "").strip() or None,
        guidance=str(snapshot.get("guidance") or "").strip() or None,
        policy_error=str(snapshot.get("policy_error") or "").strip() or None,
        label_actions=(),
    )
