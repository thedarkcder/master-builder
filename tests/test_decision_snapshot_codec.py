from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_state_machine import (
    DecisionResultSnapshot,
    PrecheckSnapshot,
    apply_frozen_cycle_questions,
    build_question_set,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult


def _precheck() -> PreRunCheckResult:
    return PreRunCheckResult(
        outcome="decision_gate_required",
        ready_label="agent:ready",
        ready_label_present=True,
        required_worker_capability="linux",
        required_worker_label="worker:linux",
        required_worker_label_present=True,
        decision_gate=DecisionGateResult(
            triggered=True,
            reason="Need owner decision",
            missing_sections=("decision_owner",),
            questions=("Who owns this?",),
            recommendation="Clarify",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=True,
            missing_criteria=(),
            clarification_questions=(),
        ),
    )


def test_precheck_snapshot_round_trip() -> None:
    snapshot = PrecheckSnapshot.from_precheck(_precheck())
    loaded = PrecheckSnapshot.load(snapshot.dump())
    assert loaded is not None
    assert loaded.to_precheck().decision_gate.reason == "Need owner decision"


def test_decision_result_snapshot_round_trip() -> None:
    decision = SimpleNamespace(
        pre_check=_precheck(),
        block_reason="decision_gate_required",
        guidance="Need decision",
        policy_error=None,
    )
    snapshot = DecisionResultSnapshot.from_decision(
        decision=decision,
        classification="decision_gate",
        issue_labels=["agent:ready"],
        missing_slots=["decision_owner"],
        auto_resolved_slots=[],
    )
    loaded = DecisionResultSnapshot.load(snapshot.dump())
    assert loaded is not None
    assert loaded.classification == "decision_gate"
    assert loaded.pre_check is not None


def test_build_question_set_is_deterministic() -> None:
    question_set = build_question_set(pre_check=_precheck(), classification="decision_gate")
    assert len(question_set) == 1
    assert question_set[0]["id"].startswith("dg_")


def test_apply_frozen_cycle_questions_restricts_to_unresolved_ids() -> None:
    pre_check = _precheck()
    updated = apply_frozen_cycle_questions(
        pre_check=pre_check,
        cycle_question_set=[
            {"id": "dg_1", "kind": "decision_gate", "text": "Who owns this?"},
            {"id": "dg_2", "kind": "decision_gate", "text": "What approval date?"},
        ],
        unresolved_question_ids=["dg_2"],
        cycle_reason="Need owner and date",
        classification="decision_gate",
    )
    assert updated.decision_gate.questions == ("What approval date?",)
