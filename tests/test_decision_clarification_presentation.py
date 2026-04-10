from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.communications.decision_clarification_presentation import (
    build_decision_clarification_presentation,
    render_decision_gate_feedback_message,
    render_decision_gate_remaining_questions_message,
)


def _decision_result(
    *,
    classification: str,
    decision_gate_reason: str,
    decision_gate_questions: tuple[str, ...] = (),
    gtd_missing_criteria: tuple[str, ...] = (),
    gtd_questions: tuple[str, ...] = (),
    missing_slots: tuple[str, ...] = (),
    auto_resolved_slots: tuple[str, ...] = (),
) -> object:
    pre_check = SimpleNamespace(
        decision_gate=SimpleNamespace(reason=decision_gate_reason, questions=decision_gate_questions),
        gtd_missing_criteria=gtd_missing_criteria,
        gtd_clarification_questions=gtd_questions,
    )
    return SimpleNamespace(
        classification=classification,
        missing_slots=list(missing_slots),
        auto_resolved_slots=list(auto_resolved_slots),
        decision=SimpleNamespace(pre_check=pre_check),
    )


def test_build_decision_clarification_presentation_prefers_feedback_questions_for_decision_gate() -> None:
    presentation = build_decision_clarification_presentation(
        decision_result=_decision_result(
            classification="decision_gate",
            decision_gate_reason="Need clarification.",
            decision_gate_questions=("Q1", "Q2"),
        ),
        question_feedback=(
            {
                "question_id": "dg_1",
                "question_text": "What is the owner?",
                "note": "Missing ownership detail",
                "status": "open",
            },
        ),
    )
    assert presentation.recheck_required is True
    assert presentation.classification == "decision_gate"
    assert presentation.questions == ("What is the owner?",)


def test_build_decision_clarification_presentation_uses_gtd_questions_when_no_feedback() -> None:
    presentation = build_decision_clarification_presentation(
        decision_result=_decision_result(
            classification="gtd",
            decision_gate_reason="Decision Gate not required.",
            gtd_missing_criteria=("Dependencies and risks identified",),
            gtd_questions=("Which dependencies or risks may impact delivery?",),
        ),
    )
    assert presentation.recheck_required is True
    assert presentation.classification == "gtd"
    assert presentation.decision_gate_reason is None
    assert presentation.questions == ("Which dependencies or risks may impact delivery?",)


def test_render_decision_gate_feedback_message_includes_missing_detail() -> None:
    message = render_decision_gate_feedback_message(
        issue_key="GP-1",
        reason="Need owner decision.",
        question_feedback=(
            {
                "question_id": "dg_1",
                "question_text": "Who owns rollout?",
                "note": "Owner role not specified",
                "status": "open",
            },
        ),
    )
    assert "Need owner decision." in message
    assert "Missing detail: Owner role not specified" in message


def test_render_decision_gate_remaining_questions_message_lists_questions() -> None:
    message = render_decision_gate_remaining_questions_message(
        issue_key="GP-1",
        reason="Need details.",
        questions=("What is the fallback?", "How do we test?"),
    )
    assert "Need details." in message
    assert "- What is the fallback?" in message
    assert "- How do we test?" in message
