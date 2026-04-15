from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_planner import DecisionPlannerQuestion, DecisionPlannerResult
from orchestrator.core.decision_state_machine import DecisionEvent
from orchestrator.core.decision_state_machine import DecisionState
from orchestrator.core.decision_state_machine import DecisionStateTransition
from orchestrator.core.decision_state_machine import ExecutionAdmissionReason
from orchestrator.core.decision_state_machine import decision_classification_for_precheck
from orchestrator.core.decision_state_machine import decision_missing_slots_for_precheck
from orchestrator.core.decision_state_machine import reduce_decision_planner_result
from orchestrator.core.decision_state_machine import resolve_worker_blocked_outcome
from orchestrator.core.decision_state_machine import resolve_worker_decision_from_precheck
from orchestrator.core.decision_state_machine import resolve_decision_state_transition
from orchestrator.core.decision_state_machine import resolve_execution_admission
from orchestrator.core.decision_state_machine import resolve_execution_gate_state
from orchestrator.core.decision_state_machine import resolve_readiness_decision
from orchestrator.core.decision_types import DecisionClassification
from orchestrator.core.decision_types import ExecutionGateState
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.runtime_payload_models import PlannerGateStatus


def _precheck(
    *,
    outcome: str,
    ready_label_present: bool = True,
) -> PreRunCheckResult:
    return PreRunCheckResult(
        outcome=outcome,
        ready_label="agent:ready",
        ready_label_present=ready_label_present,
        required_worker_capability="linux",
        required_worker_label="worker:linux",
        required_worker_label_present=True,
        decision_gate=DecisionGateResult(
            triggered=(outcome == "decision_gate_required"),
            reason="Need a decision",
            missing_sections=("owner",),
            questions=("Who owns this?",),
            recommendation="Clarify before execution.",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=(outcome not in {"gtd_required", "execution_blocked"}),
            missing_criteria=("how_to_test",) if outcome in {"gtd_required", "execution_blocked"} else (),
            clarification_questions=("How do we test this?",) if outcome in {"gtd_required", "execution_blocked"} else (),
        ),
    )


def test_resolve_decision_state_transition_open_cycle_blocked() -> None:
    transition = resolve_decision_state_transition(
        state=DecisionState(
            has_case=True,
            has_open_cycle=True,
            unresolved_question_count=2,
            decision_gate_closed_permanently=False,
            case_classification=DecisionClassification.DECISION_GATE,
            case_issue_fingerprint="f1",
            current_issue_fingerprint="f2",
        ),
        event=DecisionEvent.EVALUATE_INGRESS,
    )

    assert transition is DecisionStateTransition.OPEN_CYCLE_BLOCKED


def test_resolve_decision_state_transition_reuses_clear_fingerprint() -> None:
    transition = resolve_decision_state_transition(
        state=DecisionState(
            has_case=True,
            has_open_cycle=False,
            unresolved_question_count=0,
            decision_gate_closed_permanently=False,
            case_classification=DecisionClassification.CLEAR,
            case_issue_fingerprint="f1",
            current_issue_fingerprint="f1",
        ),
    )

    assert transition is DecisionStateTransition.REUSE_CLEAR_FINGERPRINT


def test_resolve_readiness_decision_prioritizes_policy_error() -> None:
    decision = resolve_readiness_decision(
        policy_error="runtime failed",
        block_reason="gtd_required",
        classification="gtd",
    )

    assert decision.state.value == "policy_error"
    assert decision.reason_code == "policy_eval_failed"


def test_resolve_execution_gate_state_uses_canonical_readiness_logic() -> None:
    decision = SimpleNamespace(
        pre_check=_precheck(outcome="missing_ready_label", ready_label_present=False),
        block_reason="missing_ready_label",
        guidance="Issue is missing the configured ready label.",
        policy_error=None,
    )

    resolution = resolve_execution_gate_state(decision=decision, classification="clear")

    assert resolution.state is ExecutionGateState.BLOCK_READY_LABEL
    assert resolution.reason is not None
    assert resolution.reason.reason_code == "missing_ready_label"


def test_resolve_execution_admission_blocks_from_canonical_gate_state() -> None:
    decision_result = SimpleNamespace(
        decision=SimpleNamespace(
            pre_check=_precheck(outcome="decision_gate_required"),
            block_reason="decision_gate_required",
        ),
        execution_gate=resolve_execution_gate_state(
            decision=SimpleNamespace(
                pre_check=_precheck(outcome="decision_gate_required"),
                block_reason="decision_gate_required",
                guidance=None,
                policy_error=None,
            ),
            classification="decision_gate",
        ),
    )

    admission = resolve_execution_admission(decision_result=decision_result)

    assert admission.can_enqueue is False
    assert admission.reason is ExecutionAdmissionReason.DECISION_GATE_REQUIRED
    assert admission.reason_code == "decision_gate_required"


def test_resolve_worker_decision_from_precheck_creates_blocked_gate_payload() -> None:
    worker_decision = resolve_worker_decision_from_precheck(
        pre_check=_precheck(outcome="gtd_required"),
    )

    assert worker_decision.allowed is False
    assert worker_decision.block_reason == "gtd_required"
    assert worker_decision.classification == "gtd"
    assert worker_decision.decision_gate is not None


def test_resolve_worker_blocked_outcome_handles_missing_ready_label() -> None:
    worker_decision = resolve_worker_decision_from_precheck(
        pre_check=_precheck(outcome="missing_ready_label", ready_label_present=False),
    )

    outcome = resolve_worker_blocked_outcome(worker_decision=worker_decision)

    assert outcome.block_reason == "missing_ready_label"
    assert outcome.ready_label == "agent:ready"
    assert "agent:ready" in outcome.reason


def test_decision_classification_and_missing_slots_are_canonical() -> None:
    pre_check = _precheck(outcome="gtd_required")

    assert decision_classification_for_precheck(pre_check) is DecisionClassification.GTD
    assert decision_missing_slots_for_precheck(pre_check) == ["how_to_test"]


def test_reduce_decision_planner_result_is_canonical() -> None:
    decision = SimpleNamespace(
        source="jira_webhook",
        pre_check=_precheck(outcome="decision_gate_required"),
        block_reason="decision_gate_required",
        guidance=None,
        policy_error=None,
        label_actions=(),
    )
    planner_result = DecisionPlannerResult(
        gate_status=PlannerGateStatus.BLOCKED_DECISION_GATE,
        reason="Need owner",
        questions=(
            DecisionPlannerQuestion(
                question_id="dg_owner",
                kind="decision_gate",
                question="Who owns this?",
                status="open",
                detail=None,
            ),
        ),
        question_states=(),
        resolved_items=(),
        missing_items=("decision_owner",),
        captured_answer_summary=None,
    )

    reduced = reduce_decision_planner_result(decision=decision, planner_result=planner_result)

    assert reduced.classification is DecisionClassification.DECISION_GATE
    assert reduced.question_set[0]["id"] == "dg_owner"
    assert reduced.decision.block_reason == "decision_gate_required"
