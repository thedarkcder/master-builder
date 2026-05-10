from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Iterable, Mapping

from sqlalchemy.orm import Session

from orchestrator.core.clarification.questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.runtime.runtime import CodexRuntime
from orchestrator.core.decision.engine import DecisionEngineResult
from orchestrator.core.decision.reply_service import unresolved_question_feedback_for_cycle
from orchestrator.core.decision.types import DecisionClassification
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime.invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.runtime.payload_models import PrecheckMessage


@dataclass(frozen=True)
class DecisionClarificationPresentation:
    mode: DecisionClassification
    recheck_required: bool
    decision_gate_reason: str | None
    decision_gate_questions: tuple[ClarificationQuestion, ...]
    gtd_missing_criteria: tuple[str, ...]
    gtd_questions: tuple[ClarificationQuestion, ...]
    questions: tuple[ClarificationQuestion, ...]
    question_feedback: tuple[dict[str, str], ...]
    missing_slots: tuple[str, ...]
    auto_resolved_slots: tuple[str, ...]

    @property
    def classification(self) -> str:
        return self.mode.value

    @property
    def requires_decision_gate_feedback(self) -> bool:
        return self.mode in {DecisionClassification.DECISION_GATE, DecisionClassification.BOTH}


@dataclass(frozen=True)
class DiscordDecisionClarificationPresentation:
    message: str
    response_fields: dict[str, object]


def build_runtime_precheck_message(
    *,
    runtime: CodexRuntime,
    invocation_context: AgentInvocationContext,
    issue_key: str,
    classification: DecisionClassification,
    decision_gate_reason: str,
    decision_gate_questions: list[str],
    gtd_missing_criteria: list[str],
    gtd_questions: list[str],
    missing_slots: list[str],
) -> tuple[str, list[str]]:
    payload = invoke_runtime_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("policy/precheck_message_system.j2"),
        user_prompt=render_prompt(
            "policy/precheck_message_user.j2",
            issue_key=issue_key,
            classification=classification.value,
            decision_gate_reason=decision_gate_reason,
            decision_gate_questions_json=json.dumps(decision_gate_questions),
            gtd_missing_criteria_json=json.dumps(gtd_missing_criteria),
            gtd_questions_json=json.dumps(gtd_questions),
            missing_slots_json=json.dumps(missing_slots),
        ),
    )
    parsed_payload = PrecheckMessage.from_payload(payload)
    return parsed_payload.message, list(parsed_payload.questions)


def load_cycle_question_feedback(*, session: Session, cycle_id: str | None) -> tuple[dict[str, str], ...]:
    normalized_cycle_id = str(cycle_id or "").strip()
    if not normalized_cycle_id:
        return ()
    return tuple(unresolved_question_feedback_for_cycle(session=session, cycle_id=normalized_cycle_id))


def build_decision_clarification_presentation(
    *,
    decision_result: DecisionEngineResult,
    question_feedback: Iterable[Mapping[str, Any]] = (),
) -> DecisionClarificationPresentation:
    mode = DecisionClassification.parse(getattr(decision_result, "classification", None))
    pre_check = getattr(getattr(decision_result, "decision", None), "pre_check", None)
    normalized_feedback = _normalize_question_feedback(question_feedback)
    missing_slots = tuple(
        slot
        for slot in (
            str(slot_value).strip()
            for slot_value in (getattr(decision_result, "missing_slots", ()) or ())
        )
        if slot
    )
    auto_resolved_slots = tuple(
        slot
        for slot in (
            str(slot_value).strip()
            for slot_value in (getattr(decision_result, "auto_resolved_slots", ()) or ())
        )
        if slot
    )
    if pre_check is None or mode is DecisionClassification.CLEAR:
        return DecisionClarificationPresentation(
            mode=DecisionClassification.CLEAR,
            recheck_required=False,
            decision_gate_reason=None,
            decision_gate_questions=(),
            gtd_missing_criteria=(),
            gtd_questions=(),
            questions=(),
            question_feedback=(),
            missing_slots=missing_slots,
            auto_resolved_slots=auto_resolved_slots,
        )

    decision_gate = getattr(pre_check, "decision_gate", None)
    decision_gate_reason = str(getattr(decision_gate, "reason", "") or "").strip() or None
    decision_gate_questions = ClarificationQuestionSet.from_values(
        getattr(decision_gate, "questions", ()) or ()
    ).questions
    gtd_missing_criteria = tuple(
        criteria
        for criteria in (
            str(criteria_value).strip()
            for criteria_value in (getattr(pre_check, "gtd_missing_criteria", ()) or ())
        )
        if criteria
    )
    gtd_questions = ClarificationQuestionSet.from_values(
        getattr(pre_check, "gtd_clarification_questions", ()) or ()
    ).questions

    if mode in {DecisionClassification.DECISION_GATE, DecisionClassification.BOTH} and normalized_feedback:
        questions = ClarificationQuestionSet.from_values(
            {
                "question": str(item.get("question_text") or "").strip(),
                "reason": str(item.get("note") or "").strip(),
            }
            for item in normalized_feedback
        ).questions
    else:
        questions = ClarificationQuestionSet.from_values((*decision_gate_questions, *gtd_questions)).questions

    return DecisionClarificationPresentation(
        mode=mode,
        recheck_required=True,
        decision_gate_reason=(
            decision_gate_reason if mode in {DecisionClassification.DECISION_GATE, DecisionClassification.BOTH} else None
        ),
        decision_gate_questions=decision_gate_questions,
        gtd_missing_criteria=gtd_missing_criteria,
        gtd_questions=gtd_questions,
        questions=questions,
        question_feedback=normalized_feedback,
        missing_slots=missing_slots,
        auto_resolved_slots=auto_resolved_slots,
    )


def build_decision_clarification_response_fields(
    *,
    presentation: DecisionClarificationPresentation,
    questions: Iterable[ClarificationQuestion] | None = None,
) -> dict[str, object]:
    effective_questions = ClarificationQuestionSet.from_values(questions or presentation.questions)
    return {
        "classification": presentation.mode.value,
        "decision_gate_reason": presentation.decision_gate_reason,
        "gtd_missing_criteria": list(presentation.gtd_missing_criteria),
        "questions": list(effective_questions.prompts),
        "question_feedback": list(presentation.question_feedback),
        "missing_slots": list(presentation.missing_slots),
        "auto_resolved_slots": list(presentation.auto_resolved_slots),
    }


def present_discord_decision_clarification(
    *,
    issue_key: str,
    presentation: DecisionClarificationPresentation,
    precheck_message_builder: Callable[[], tuple[str, list[str]]],
) -> DiscordDecisionClarificationPresentation:
    if presentation.requires_decision_gate_feedback and presentation.question_feedback:
        message = render_decision_gate_feedback_message(
            issue_key=issue_key,
            reason=presentation.decision_gate_reason or "clarification required",
            question_feedback=presentation.question_feedback,
        )
        generated_questions = presentation.questions
    elif presentation.requires_decision_gate_feedback:
        message = render_decision_gate_remaining_questions_message(
            issue_key=issue_key,
            reason=presentation.decision_gate_reason or "clarification required",
            questions=presentation.decision_gate_questions,
        )
        generated_questions = presentation.questions
    else:
        message, generated_questions = precheck_message_builder()
        generated_questions = ClarificationQuestionSet.from_values(generated_questions).questions
    return DiscordDecisionClarificationPresentation(
        message=message,
        response_fields=build_decision_clarification_response_fields(
            presentation=presentation,
            questions=generated_questions,
        ),
    )


def render_decision_gate_remaining_questions_message(
    *,
    issue_key: str,
    reason: str,
    questions: Iterable[object],
) -> str:
    lines = [
        f"Decision Gate still needs clarification for `{issue_key}`.",
        f"Reason: {reason}",
    ]
    normalized_questions = ClarificationQuestionSet.from_values(questions)
    if normalized_questions:
        lines.append("Please reply with:")
        lines.extend(normalized_questions.render_lines(limit=5))
    return "\n".join(lines)


def render_decision_gate_feedback_message(
    *,
    issue_key: str,
    reason: str,
    question_feedback: Iterable[Mapping[str, Any]],
) -> str:
    lines = [
        f"Decision Gate still needs clarification for `{issue_key}`.",
        f"Reason: {reason}",
    ]
    feedback = _normalize_question_feedback(question_feedback)
    if feedback:
        lines.append("Please reply with:")
    for item in feedback[:5]:
        question_text = str(item.get("question_text") or "").strip()
        note = str(item.get("note") or "").strip()
        if question_text:
            lines.append(f"- {question_text}")
        if note:
            lines.append(f"  Missing detail: {note}")
    return "\n".join(lines)


def _normalize_question_feedback(
    question_feedback: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    normalized: list[dict[str, str]] = []
    for item in question_feedback:
        question_id = str(item.get("question_id") or "").strip()
        question_text = str(item.get("question_text") or "").strip()
        note = str(item.get("note") or "").strip()
        status = str(item.get("status") or "").strip()
        if not question_id and not question_text and not note and not status:
            continue
        normalized.append(
            {
                "question_id": question_id,
                "question_text": question_text,
                "note": note,
                "status": status,
            }
        )
    return tuple(normalized)
