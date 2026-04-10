from __future__ import annotations

from dataclasses import dataclass


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


@dataclass(frozen=True)
class DecisionPlannerQuestionPayload:
    question_id: str
    kind: str
    question: str
    status: str
    detail: str | None

    @classmethod
    def from_payload(cls, payload: object, *, default_kind: str) -> DecisionPlannerQuestionPayload | None:
        if not isinstance(payload, dict):
            return None
        question_id = str(payload.get("question_id") or "").strip()
        question = str(payload.get("question") or "").strip()
        if not question_id or not question:
            return None

        kind = str(payload.get("kind") or "").strip().lower() or default_kind
        if kind not in {"decision_gate", "gtd"}:
            kind = "decision_gate"

        status = str(payload.get("status") or "").strip().lower() or "open"
        if status not in {"open", "answered", "accepted"}:
            status = "open"

        return cls(
            question_id=question_id,
            kind=kind,
            question=question,
            status=status,
            detail=str(payload.get("detail") or "").strip() or None,
        )


@dataclass(frozen=True)
class DecisionPlannerPayload:
    gate_status: str
    reason: str
    questions: tuple[DecisionPlannerQuestionPayload, ...]
    question_states: tuple[DecisionPlannerQuestionPayload, ...]
    resolved_items: tuple[str, ...]
    missing_items: tuple[str, ...]
    captured_answer_summary: str | None

    @classmethod
    def from_payload(cls, payload: object, *, classification: str) -> DecisionPlannerPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Decision planner returned non-object payload")
        gate_status = str(payload.get("gate_status") or "").strip().lower()
        if gate_status not in {"clear", "blocked_decision_gate", "blocked_gtd", "blocked_both"}:
            raise RuntimeError("Decision planner returned invalid gate_status")
        reason = str(payload.get("reason") or "").strip()
        if gate_status != "clear" and not reason:
            raise RuntimeError("Decision planner returned blocked state without reason")

        from orchestrator.core.decision_types import DecisionClassification

        parsed_classification = DecisionClassification.parse(classification)
        default_kind = "gtd" if parsed_classification is DecisionClassification.GTD else "decision_gate"
        questions = tuple(
            item
            for raw_item in (payload.get("questions") if isinstance(payload.get("questions"), list) else [])
            for item in [DecisionPlannerQuestionPayload.from_payload(raw_item, default_kind=default_kind)]
            if item is not None
        )
        question_states = tuple(
            item
            for raw_item in (
                payload.get("question_states") if isinstance(payload.get("question_states"), list) else []
            )
            for item in [DecisionPlannerQuestionPayload.from_payload(raw_item, default_kind=default_kind)]
            if item is not None
        )
        if not question_states:
            question_states = questions

        return cls(
            gate_status=gate_status,
            reason=reason,
            questions=questions,
            question_states=question_states,
            resolved_items=_string_tuple(payload.get("resolved_items")),
            missing_items=_string_tuple(payload.get("missing_items")),
            captured_answer_summary=str(payload.get("captured_answer_summary") or "").strip() or None,
        )


@dataclass(frozen=True)
class PrecheckPolicyPayload:
    decision_gate_triggered: bool
    decision_gate_reason: str
    decision_gate_missing_sections: tuple[str, ...]
    decision_gate_questions: tuple[str, ...]
    decision_gate_recommendation: str
    decision_gate_tags: tuple[str, ...]
    gtd_valid: bool
    gtd_missing_criteria: tuple[str, ...]
    gtd_clarification_questions: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object) -> PrecheckPolicyPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Codex precheck policy evaluation returned non-object payload")
        decision_gate_reason = str(payload.get("reason") or "").strip()
        decision_gate_recommendation = str(payload.get("recommendation") or "").strip()
        if not decision_gate_reason:
            raise RuntimeError("Codex precheck policy evaluation returned empty decision_gate reason")
        if not decision_gate_recommendation:
            raise RuntimeError("Codex precheck policy evaluation returned empty decision_gate recommendation")

        gtd_valid = bool(payload.get("gtd_valid"))
        gtd_clarification_questions = _string_tuple(payload.get("gtd_clarification_questions"))
        if not gtd_valid and not gtd_clarification_questions:
            raise RuntimeError(
                "Codex precheck policy evaluation returned invalid GTD result without clarification questions"
            )

        return cls(
            decision_gate_triggered=bool(payload.get("triggered")),
            decision_gate_reason=decision_gate_reason,
            decision_gate_missing_sections=_string_tuple(payload.get("missing_sections")),
            decision_gate_questions=_string_tuple(payload.get("questions")),
            decision_gate_recommendation=decision_gate_recommendation,
            decision_gate_tags=_string_tuple(payload.get("tags")),
            gtd_valid=gtd_valid,
            gtd_missing_criteria=_string_tuple(payload.get("gtd_missing_criteria")),
            gtd_clarification_questions=gtd_clarification_questions,
        )


@dataclass(frozen=True)
class PrecheckMessagePayload:
    message: str
    questions: tuple[str, ...]
    classification: str

    @classmethod
    def from_payload(cls, payload: object) -> PrecheckMessagePayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Precheck message payload is not an object")
        message = str(payload.get("message") or "").strip()
        questions = tuple(_string_list(payload.get("questions")))
        classification = str(payload.get("classification") or "").strip().lower()
        if not message:
            raise RuntimeError("Precheck message payload missing message")
        if classification not in {"decision_gate", "gtd", "both", "clear"}:
            raise RuntimeError("Precheck message payload has invalid classification")
        return cls(
            message=message,
            questions=questions,
            classification=classification,
        )
