from __future__ import annotations

from typing import Any

from orchestrator.core.decision_state_machine import DecisionEvent
from orchestrator.core.decision_state_machine import DecisionState
from orchestrator.core.decision_state_machine import DecisionStateTransition
from orchestrator.core.decision_state_machine import case_state_for_decision as canonical_case_state_for_decision
from orchestrator.core.decision_state_machine import decision_from_snapshot as canonical_decision_from_snapshot
from orchestrator.core.decision_state_machine import decision_reason as canonical_decision_reason
from orchestrator.core.decision_state_machine import resolve_decision_state_transition
from orchestrator.core.decision_types import (
    DecisionClassification,
    IngressDecision,
    PrecheckOutcome,
)
from orchestrator.storage.models import DecisionCase, DecisionCycle


DecisionStateReducerInput = DecisionState


def reduce_decision_state_transition(*, input_state: DecisionStateReducerInput) -> DecisionStateTransition:
    return resolve_decision_state_transition(
        state=input_state,
        event=DecisionEvent.EVALUATE_INGRESS,
    )


def case_state_for_decision(*, decision: IngressDecision) -> str:
    return canonical_case_state_for_decision(decision=decision)


def decision_reason(*, pre_check: object, classification: str) -> str | None:
    return canonical_decision_reason(pre_check=pre_check, classification=classification)


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
    return canonical_decision_from_snapshot(
        snapshot=snapshot,
        source=source,
        classification=classification,
        cycle=cycle,
        case=case,
    )
