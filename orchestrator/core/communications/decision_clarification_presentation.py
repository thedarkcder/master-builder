from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping

from sqlalchemy.orm import Session

from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.decision_reply_service import unresolved_question_feedback_for_cycle


class ClarificationMode(str, Enum):
    CLEAR = "clear"
    DECISION_GATE = "decision_gate"
    GTD = "gtd"
    BOTH = "both"


@dataclass(frozen=True)
class DecisionClarificationPresentation:
    mode: ClarificationMode
    recheck_required: bool
    decision_gate_reason: str | None
    decision_gate_questions: tuple[str, ...]
    gtd_missing_criteria: tuple[str, ...]
    gtd_questions: tuple[str, ...]
    questions: tuple[str, ...]
    question_feedback: tuple[dict[str, str], ...]
    missing_slots: tuple[str, ...]
    auto_resolved_slots: tuple[str, ...]

    @property
    def classification(self) -> str:
        return self.mode.value

    @property
    def requires_decision_gate_feedback(self) -> bool:
        return self.mode in {ClarificationMode.DECISION_GATE, ClarificationMode.BOTH}


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
    raw_classification = str(getattr(decision_result, "classification", "") or "").strip().lower()
    try:
        mode = ClarificationMode(raw_classification or ClarificationMode.CLEAR.value)
    except ValueError:
        mode = ClarificationMode.CLEAR
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
    if pre_check is None or mode is ClarificationMode.CLEAR:
        return DecisionClarificationPresentation(
            mode=ClarificationMode.CLEAR,
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
    decision_gate_questions = tuple(
        question
        for question in (
            str(question_value).strip()
            for question_value in (getattr(decision_gate, "questions", ()) or ())
        )
        if question
    )
    gtd_missing_criteria = tuple(
        criteria
        for criteria in (
            str(criteria_value).strip()
            for criteria_value in (getattr(pre_check, "gtd_missing_criteria", ()) or ())
        )
        if criteria
    )
    gtd_questions = tuple(
        question
        for question in (
            str(question_value).strip()
            for question_value in (getattr(pre_check, "gtd_clarification_questions", ()) or ())
        )
        if question
    )

    if mode in {ClarificationMode.DECISION_GATE, ClarificationMode.BOTH} and normalized_feedback:
        questions = _dedupe(
            tuple(
                question_text
                for question_text in (
                    str(item.get("question_text") or "").strip() for item in normalized_feedback
                )
                if question_text
            )
        )
    else:
        questions = _dedupe((*decision_gate_questions, *gtd_questions))

    return DecisionClarificationPresentation(
        mode=mode,
        recheck_required=True,
        decision_gate_reason=(
            decision_gate_reason if mode in {ClarificationMode.DECISION_GATE, ClarificationMode.BOTH} else None
        ),
        decision_gate_questions=decision_gate_questions,
        gtd_missing_criteria=gtd_missing_criteria,
        gtd_questions=gtd_questions,
        questions=questions,
        question_feedback=normalized_feedback,
        missing_slots=missing_slots,
        auto_resolved_slots=auto_resolved_slots,
    )


def render_decision_gate_remaining_questions_message(
    *,
    issue_key: str,
    reason: str,
    questions: Iterable[str],
) -> str:
    lines = [
        f"Decision Gate still needs clarification for `{issue_key}`.",
        f"Reason: {reason}",
    ]
    normalized_questions = [str(item).strip() for item in questions if str(item).strip()]
    if normalized_questions:
        lines.append("Please reply with:")
        lines.extend(f"- {question}" for question in normalized_questions[:5])
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


def _dedupe(items: Iterable[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in items:
        normalized = str(value).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return tuple(ordered)
