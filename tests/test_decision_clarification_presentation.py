from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.communications.decision_clarification_presentation import (
    build_decision_clarification_presentation,
    build_decision_clarification_response_fields,
    build_runtime_precheck_message,
    present_discord_decision_clarification,
    render_decision_gate_feedback_message,
    render_decision_gate_remaining_questions_message,
)
from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.runtime.invocation import AgentInvocationContext
from orchestrator.core.decision.types import DecisionClassification


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
    assert presentation.mode is DecisionClassification.DECISION_GATE
    assert presentation.classification == "decision_gate"
    assert tuple(question.question for question in presentation.questions) == ("What is the owner?",)
    assert presentation.questions[0].why_it_matters == "Missing ownership detail"


def test_build_decision_clarification_presentation_excludes_accepted_feedback_questions() -> None:
    presentation = build_decision_clarification_presentation(
        decision_result=_decision_result(
            classification="decision_gate",
            decision_gate_reason="Need clarification.",
            decision_gate_questions=("Q1", "Q2"),
        ),
        question_feedback=(
            {
                "question_id": "objective",
                "question_text": "What is the objective?",
                "note": "Already present in Jira.",
                "status": "accepted",
            },
            {
                "question_id": "owner",
                "question_text": "Who owns approval?",
                "note": "Missing owner.",
                "status": "open",
            },
        ),
    )

    assert tuple(question.question for question in presentation.questions) == ("Who owns approval?",)
    assert [item["question_id"] for item in presentation.question_feedback] == ["owner"]


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
    assert presentation.mode is DecisionClassification.GTD
    assert presentation.classification == "gtd"
    assert presentation.decision_gate_reason is None
    assert tuple(question.question for question in presentation.questions) == (
        "Which dependencies or risks may impact delivery?",
    )


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
    assert "What is still missing:" in message
    assert "Owner role not specified" in message
    assert "Missing detail:" not in message
    assert "Please reply with:" not in message


def test_render_decision_gate_feedback_message_humanizes_partial_answers() -> None:
    message = render_decision_gate_feedback_message(
        issue_key="AP-248",
        reason="AP-248 still needs clarifications before it is fully clear.",
        question_feedback=(
            {
                "question_id": "decision_owner",
                "question_text": "Who is the single accountable decision owner for AP-248?",
                "answer": "stake holder",
                "note": "Current answer 'stake holder' is not a specific accountable person.",
                "status": "answered",
            },
            {
                "question_id": "hubspot_subscription_rules",
                "question_text": "Which HubSpot information should decide whether the customer subscription is active, overdue, cancelled, or expired?",
                "note": "Annual invoice is confirmed as billing source, but the business rules for subscription status are not clear yet.",
                "status": "answered",
            },
        ),
    )

    assert "I still need a bit more before I can run `AP-248`." in message
    assert "Owner: I have `stake holder`, but that is not a specific accountable person." in message
    assert "HubSpot subscription rules: Annual invoice is confirmed as billing source" in message
    assert "Reply in plain English" not in message
    assert "Please reply with:" not in message
    assert "Missing detail:" not in message


def test_render_decision_gate_remaining_questions_message_lists_questions() -> None:
    message = render_decision_gate_remaining_questions_message(
        issue_key="GP-1",
        reason="Need details.",
        questions=("What is the fallback?", "How do we test?"),
    )
    assert "Need details." in message
    assert "I need a bit more before I can run `GP-1`." in message
    assert "I need:" in message
    assert "- What is the fallback?" in message
    assert "- How do we test?" in message


def test_build_decision_clarification_response_fields_serializes_typed_presentation() -> None:
    presentation = build_decision_clarification_presentation(
        decision_result=_decision_result(
            classification="gtd",
            decision_gate_reason="Decision Gate not required.",
            gtd_missing_criteria=("Dependencies and risks identified",),
            gtd_questions=("Which dependencies or risks may impact delivery?",),
            missing_slots=("dependencies",),
            auto_resolved_slots=("objective",),
        ),
    )

    response_fields = build_decision_clarification_response_fields(
        presentation=presentation,
    )

    assert response_fields == {
        "classification": "gtd",
        "decision_gate_reason": None,
        "gtd_missing_criteria": ["Dependencies and risks identified"],
        "questions": ["Which dependencies or risks may impact delivery?"],
        "question_feedback": [],
        "missing_slots": ["dependencies"],
        "auto_resolved_slots": ["objective"],
    }


def test_present_discord_decision_clarification_prefers_feedback_rendering() -> None:
    presentation = build_decision_clarification_presentation(
        decision_result=_decision_result(
            classification="decision_gate",
            decision_gate_reason="Need owner decision.",
            decision_gate_questions=("Who owns rollout?",),
        ),
        question_feedback=(
            {
                "question_id": "dg_1",
                "question_text": "Who owns rollout?",
                "note": "Owner role not specified",
                "status": "open",
            },
        ),
    )

    result = present_discord_decision_clarification(
        issue_key="GP-1",
        presentation=presentation,
        precheck_message_builder=lambda: ("fallback message", ["fallback question"]),
    )

    assert "Need owner decision." in result.message
    assert "Who owns rollout: Owner role not specified" in result.message
    assert "Missing detail:" not in result.message
    assert result.response_fields["classification"] == "decision_gate"
    assert result.response_fields["questions"] == ["Who owns rollout?"]


def test_build_runtime_precheck_message_raises_when_runtime_payload_is_invalid() -> None:
    runtime = SimpleNamespace()
    with patch(
        "orchestrator.core.communications.decision_clarification_presentation.invoke_runtime_json",
        return_value={"message": "Need clarification", "questions": [], "classification": "invalid"},
    ):
        try:
            build_runtime_precheck_message(
                runtime=runtime,
                invocation_context=AgentInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    command="reply",
                    stage="precheck_message",
                    working_dir=".",
                    issue_key="GP-1",
                ),
                issue_key="GP-1",
                classification=DecisionClassification.GTD,
                decision_gate_reason="",
                decision_gate_questions=[],
                gtd_missing_criteria=["dependencies_and_risks"],
                gtd_questions=["Which dependencies or risks may impact delivery?"],
                missing_slots=["dependencies_and_risks"],
            )
        except RuntimeError as exc:
            assert "invalid classification" in str(exc)
        else:
            raise AssertionError("invalid runtime payload should raise")


def test_build_runtime_precheck_message_raises_runtime_errors_without_local_fallback() -> None:
    runtime = SimpleNamespace()
    with patch(
        "orchestrator.core.communications.decision_clarification_presentation.invoke_runtime_json",
        side_effect=CodexRuntimeError("runtime failed"),
    ):
        try:
            build_runtime_precheck_message(
                runtime=runtime,
                invocation_context=AgentInvocationContext(
                    channel="discord",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    command="reply",
                    stage="precheck_message",
                    working_dir=".",
                    issue_key="GP-1",
                ),
                issue_key="GP-1",
                classification=DecisionClassification.GTD,
                decision_gate_reason="",
                decision_gate_questions=[],
                gtd_missing_criteria=["dependencies_and_risks"],
                gtd_questions=["Which dependencies or risks may impact delivery?"],
                missing_slots=["dependencies_and_risks"],
            )
        except CodexRuntimeError as exc:
            assert "runtime failed" in str(exc)
        else:
            raise AssertionError("runtime errors should propagate")
