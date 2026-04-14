from __future__ import annotations

from dataclasses import dataclass, replace

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_planner import DecisionPlannerResult
from orchestrator.core.decision_state_machine import blocking_reason_for_precheck
from orchestrator.core.decision_state_machine import guidance_for_precheck_block_reason
from orchestrator.core.decision_types import (
    DecisionClassification,
    IngressDecision,
    PrecheckOutcome,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.runtime_payload_models import PlannerGateStatus
from orchestrator.storage.models import DecisionCase


@dataclass(frozen=True)
class ReducedPlannerDecision:
    decision: IngressDecision
    classification: DecisionClassification
    question_set: list[dict[str, object]]
    question_states: list[dict[str, object]]


def planner_classification(*, gate_status: PlannerGateStatus) -> DecisionClassification:
    if gate_status is PlannerGateStatus.BLOCKED_DECISION_GATE:
        return DecisionClassification.DECISION_GATE
    if gate_status is PlannerGateStatus.BLOCKED_GTD:
        return DecisionClassification.GTD
    if gate_status is PlannerGateStatus.BLOCKED_BOTH:
        return DecisionClassification.BOTH
    return DecisionClassification.CLEAR


def clear_planner_result() -> DecisionPlannerResult:
    return DecisionPlannerResult(
        gate_status=PlannerGateStatus.CLEAR,
        reason="",
        questions=(),
        question_states=(),
        resolved_items=(),
        missing_items=(),
        captured_answer_summary=None,
    )


def reduce_planner_result(
    *,
    decision: IngressDecision,
    planner_result: DecisionPlannerResult,
) -> ReducedPlannerDecision:
    reduced_decision = _decision_with_planner_result(decision=decision, planner_result=planner_result)
    return ReducedPlannerDecision(
        decision=reduced_decision,
        classification=planner_classification(gate_status=planner_result.gate_status),
        question_set=_planner_question_set(planner_result),
        question_states=_planner_question_state_payload(planner_result),
    )


def coerce_clear_decision(*, decision: IngressDecision, case: DecisionCase) -> IngressDecision:
    normalized_pre_check = (
        decision.pre_check
        if isinstance(decision.pre_check, PreRunCheckResult)
        else _synthetic_clear_pre_check(case=case)
    )
    reduced = reduce_planner_result(
        decision=IngressDecision(
            source=decision.source,
            pre_check=normalized_pre_check,
            block_reason=blocking_reason_for_precheck(normalized_pre_check),
            guidance=None,
            policy_error=None,
            label_actions=decision.label_actions,
        ),
        planner_result=clear_planner_result(),
    )
    return reduced.decision


def _planner_question_set(planner_result: DecisionPlannerResult) -> list[dict[str, object]]:
    open_question_overrides = {
        item.question_id: item
        for item in planner_result.questions
        if str(item.question_id or "").strip()
    }
    states = planner_result.question_states or planner_result.questions
    question_set: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in states:
        if item.question_id in seen:
            continue
        seen.add(item.question_id)
        override = open_question_overrides.get(item.question_id)
        question_set.append(
            {
                "id": item.question_id,
                "kind": override.kind if override is not None else item.kind,
                "text": override.question if override is not None else item.question,
                "status": item.status,
                "detail": override.detail if override is not None else item.detail,
            }
        )
    return question_set


def _planner_question_state_payload(planner_result: DecisionPlannerResult) -> list[dict[str, object]]:
    open_question_overrides = {
        item.question_id: item
        for item in planner_result.questions
        if str(item.question_id or "").strip()
    }
    return [
        {
            "question_id": item.question_id,
            "kind": (
                open_question_overrides[item.question_id].kind
                if item.question_id in open_question_overrides
                else item.kind
            ),
            "question": (
                open_question_overrides[item.question_id].question
                if item.question_id in open_question_overrides
                else item.question
            ),
            "status": item.status,
            "detail": (
                open_question_overrides[item.question_id].detail
                if item.question_id in open_question_overrides
                else item.detail
            ),
        }
        for item in (planner_result.question_states or planner_result.questions)
    ]


def _planner_block_reason(classification: DecisionClassification) -> str | None:
    if classification.includes_decision_gate:
        return PrecheckOutcome.DECISION_GATE_REQUIRED.value
    if classification is DecisionClassification.GTD:
        return PrecheckOutcome.GTD_REQUIRED.value
    return None


def _decision_with_planner_result(
    *,
    decision: IngressDecision,
    planner_result: DecisionPlannerResult,
) -> IngressDecision:
    pre_check = decision.pre_check
    if not isinstance(pre_check, PreRunCheckResult):
        return decision
    classification = planner_classification(gate_status=planner_result.gate_status)
    reason = planner_result.reason
    decision_gate_questions = tuple(
        item.question
        for item in planner_result.questions
        if item.kind == "decision_gate"
    )
    gtd_questions = tuple(
        item.question
        for item in planner_result.questions
        if item.kind == "gtd"
    )
    missing_items = tuple(planner_result.missing_items)
    if classification is DecisionClassification.CLEAR:
        outcome = (
            PrecheckOutcome.MISSING_READY_LABEL.value
            if pre_check.ready_label_missing
            else PrecheckOutcome.READY_FOR_AGENT.value
        )
        updated_pre_check = replace(
            pre_check,
            outcome=outcome,
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
        updated_block_reason = blocking_reason_for_precheck(updated_pre_check)
        return IngressDecision(
            source=decision.source,
            pre_check=updated_pre_check,
            block_reason=updated_block_reason,
            guidance=guidance_for_precheck_block_reason(block_reason=updated_block_reason),
            policy_error=None,
            label_actions=decision.label_actions,
        )

    updated_pre_check = replace(
        pre_check,
        outcome=(
            PrecheckOutcome.DECISION_GATE_REQUIRED.value
            if classification.includes_decision_gate
            else PrecheckOutcome.GTD_REQUIRED.value
        ),
        decision_gate=DecisionGateResult(
            triggered=classification.includes_decision_gate,
            reason=reason if classification.includes_decision_gate else "Decision Gate not required",
            missing_sections=missing_items if classification.includes_decision_gate else (),
            questions=decision_gate_questions,
            recommendation=(
                "Clarification required before execution."
                if classification.includes_decision_gate
                else pre_check.decision_gate.recommendation
            ),
            tags=pre_check.decision_gate.tags,
        ),
        gtd=GoodToDoValidationResult(
            valid=not classification.includes_gtd,
            missing_criteria=missing_items if classification.includes_gtd else (),
            clarification_questions=gtd_questions,
        ),
    )
    block_reason = _planner_block_reason(classification)
    return IngressDecision(
        source=decision.source,
        pre_check=updated_pre_check,
        block_reason=block_reason,
        guidance=guidance_for_precheck_block_reason(block_reason=block_reason),
        policy_error=decision.policy_error,
        label_actions=decision.label_actions,
    )


def _synthetic_clear_pre_check(*, case: DecisionCase) -> PreRunCheckResult:
    ready_label = str(case.ready_label or "").strip() or None
    ready_label_present = bool(case.ready_label_present)
    outcome = (
        PrecheckOutcome.MISSING_READY_LABEL.value
        if ready_label and not ready_label_present
        else PrecheckOutcome.READY_FOR_AGENT.value
    )
    return PreRunCheckResult(
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
