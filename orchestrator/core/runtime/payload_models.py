from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from orchestrator.core.clarification.questions import ClarificationQuestion


class PlannerGateStatus(str, Enum):
    CLEAR = "clear"
    BLOCKED_DECISION_GATE = "blocked_decision_gate"
    BLOCKED_GTD = "blocked_gtd"
    BLOCKED_BOTH = "blocked_both"

    @classmethod
    def parse(cls, value: object) -> PlannerGateStatus:
        normalized = str(getattr(value, "value", value) or "").strip().lower()
        for item in cls:
            if normalized == item.value:
                return item
        raise ValueError(f"Unsupported planner gate_status: {value!r}")


def _strict_string_tuple(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise RuntimeError(f"{context} has invalid list")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError(f"{context} has invalid list item")
        stripped = item.strip()
        if stripped:
            normalized.append(stripped)
    return tuple(normalized)


def _normalized_string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError("Payload string list contains non-string item")
        text = " ".join(item.split())
        if text:
            normalized.append(text)
    return tuple(normalized)


def _optional_string_tuple(payload: dict[str, object], key: str, *, context: str) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list):
        raise RuntimeError(f"{context} has invalid {key}")
    return _normalized_string_tuple(value)


def _normalized_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized or None


def _require_string(payload: dict[str, object], key: str, *, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise RuntimeError(f"{context} missing {key}")
    normalized = value.strip()
    if not normalized:
        raise RuntimeError(f"{context} missing {key}")
    return normalized


def _require_optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"Payload field {key} must be a string when present")
    normalized = value.strip()
    return normalized or None


def _require_string_list(payload: dict[str, object], key: str, *, context: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise RuntimeError(f"{context} missing {key}")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError(f"{context} has invalid {key} item")
        stripped = item.strip()
        if not stripped:
            raise RuntimeError(f"{context} has empty {key} item")
        normalized.append(stripped)
    return normalized


def _require_string_tuple(payload: dict[str, object], key: str, *, context: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise RuntimeError(f"{context} missing {key}")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RuntimeError(f"{context} has invalid {key} item")
        stripped = " ".join(item.split())
        if not stripped:
            raise RuntimeError(f"{context} has empty {key} item")
        normalized.append(stripped)
    return tuple(normalized)


def _require_dict(payload: dict[str, object], key: str, *, context: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise RuntimeError(f"{context} missing {key}")
    return value


def _require_dict_list(payload: dict[str, object], key: str, *, context: str) -> list[dict[str, object]]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise RuntimeError(f"{context} missing {key}")
    items: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise RuntimeError(f"{context} has invalid {key} item")
        items.append(item)
    return items


def _require_planning_text_field(
    *,
    planning_state: str,
    issue_index: int,
    field_name: str,
    raw_value: object,
) -> str:
    value = _normalized_optional_text(raw_value)
    if value:
        return value
    raise RuntimeError(f"Codex returned {planning_state} child_ticket_specs[{issue_index}] without {field_name}")


def _require_planning_string_tuple_field(
    *,
    planning_state: str,
    issue_index: int,
    field_name: str,
    raw_value: object,
) -> tuple[str, ...]:
    if not isinstance(raw_value, list):
        raise RuntimeError(
            f"Codex returned {planning_state} child_ticket_specs[{issue_index}] "
            f"with invalid {field_name}; expected a non-empty array of strings"
        )
    values = _normalized_string_tuple(raw_value)
    if values:
        return values
    raise RuntimeError(
        f"Codex returned {planning_state} child_ticket_specs[{issue_index}] "
        f"with empty {field_name}; expected a non-empty array of strings"
    )


@dataclass(frozen=True)
class DecisionPlannerQuestionPayload:
    question_id: str
    kind: str
    question: str
    status: str
    detail: str | None

    @classmethod
    def from_payload(cls, payload: object, *, default_kind: str, context: str) -> DecisionPlannerQuestionPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        question_id = str(payload.get("question_id") or "").strip()
        question = str(payload.get("question") or "").strip()
        if not question_id or not question:
            raise RuntimeError(f"{context} missing question_id or question")

        kind = str(payload.get("kind") or "").strip().lower() or default_kind
        if kind not in {"decision_gate", "gtd"}:
            raise RuntimeError("Decision planner question has invalid kind")

        status = str(payload.get("status") or "").strip().lower() or "open"
        if status not in {"open", "answered", "accepted"}:
            raise RuntimeError("Decision planner question has invalid status")

        return cls(
            question_id=question_id,
            kind=kind,
            question=question,
            status=status,
            detail=str(payload.get("detail") or "").strip() or None,
        )


@dataclass(frozen=True)
class DecisionPlannerPayload:
    gate_status: PlannerGateStatus
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
        try:
            gate_status = PlannerGateStatus.parse(payload.get("gate_status"))
        except ValueError as exc:
            raise RuntimeError("Decision planner returned invalid gate_status") from exc
        reason = str(payload.get("reason") or "").strip()
        if gate_status is not PlannerGateStatus.CLEAR and not reason:
            raise RuntimeError("Decision planner returned blocked state without reason")

        from orchestrator.core.decision.types import DecisionClassification

        parsed_classification = DecisionClassification.parse(classification)
        default_kind = "gtd" if parsed_classification is DecisionClassification.GTD else "decision_gate"
        raw_questions = payload.get("questions")
        if not isinstance(raw_questions, list):
            raise RuntimeError("Decision planner returned invalid questions")
        raw_question_states = payload.get("question_states")
        if not isinstance(raw_question_states, list):
            raise RuntimeError("Decision planner returned invalid question_states")
        questions = tuple(
            DecisionPlannerQuestionPayload.from_payload(
                raw_item,
                default_kind=default_kind,
                context=f"Decision planner questions[{index}]",
            )
            for index, raw_item in enumerate(raw_questions, start=1)
        )
        question_states = tuple(
            DecisionPlannerQuestionPayload.from_payload(
                raw_item,
                default_kind=default_kind,
                context=f"Decision planner question_states[{index}]",
            )
            for index, raw_item in enumerate(raw_question_states, start=1)
        )
        if not question_states:
            question_states = questions

        return cls(
            gate_status=gate_status,
            reason=reason,
            questions=questions,
            question_states=question_states,
            resolved_items=_strict_string_tuple(
                payload.get("resolved_items"),
                context="Decision planner resolved_items",
            ),
            missing_items=_strict_string_tuple(
                payload.get("missing_items"),
                context="Decision planner missing_items",
            ),
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
        gtd_clarification_questions = _strict_string_tuple(
            payload.get("gtd_clarification_questions"),
            context="Codex precheck policy evaluation gtd_clarification_questions",
        )
        if not gtd_valid and not gtd_clarification_questions:
            raise RuntimeError(
                "Codex precheck policy evaluation returned invalid GTD result without clarification questions"
            )

        return cls(
            decision_gate_triggered=bool(payload.get("triggered")),
            decision_gate_reason=decision_gate_reason,
            decision_gate_missing_sections=_strict_string_tuple(
                payload.get("missing_sections"),
                context="Codex precheck policy evaluation missing_sections",
            ),
            decision_gate_questions=_strict_string_tuple(
                payload.get("questions"),
                context="Codex precheck policy evaluation questions",
            ),
            decision_gate_recommendation=decision_gate_recommendation,
            decision_gate_tags=_strict_string_tuple(
                payload.get("tags"),
                context="Codex precheck policy evaluation tags",
            ),
            gtd_valid=gtd_valid,
            gtd_missing_criteria=_strict_string_tuple(
                payload.get("gtd_missing_criteria"),
                context="Codex precheck policy evaluation gtd_missing_criteria",
            ),
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
        questions = _strict_string_tuple(payload.get("questions"), context="Precheck message payload questions")
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


@dataclass(frozen=True)
class VoiceEntryRoutePayload:
    lane: str
    persona: str
    confidence: float
    reason: str

    @classmethod
    def from_payload(cls, payload: object) -> VoiceEntryRoutePayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Voice entry router returned non-object payload")
        lane = _require_string(payload, "lane", context="Voice entry router payload").lower()
        if lane not in {"ask", "interview"}:
            raise RuntimeError("Voice entry router returned invalid lane")
        persona = _require_string(payload, "persona", context="Voice entry router payload").lower()
        if persona not in {"pm", "architect", "engineer", "qa", "security"}:
            raise RuntimeError("Voice entry router returned invalid persona")
        confidence_raw = payload.get("confidence")
        if not isinstance(confidence_raw, (int, float)):
            raise RuntimeError("Voice entry router returned invalid confidence")
        confidence = float(confidence_raw)
        if confidence < 0.0 or confidence > 1.0:
            raise RuntimeError("Voice entry router returned invalid confidence")
        reason = _require_string(payload, "reason", context="Voice entry router payload")
        if lane == "interview" and persona != "pm":
            raise RuntimeError("Voice entry router returned interview lane without pm persona")
        return cls(lane=lane, persona=persona, confidence=confidence, reason=reason)


@dataclass(frozen=True)
class JiraIssueIntakeRoutePayload:
    route: str
    reason: str
    confidence: str

    @classmethod
    def from_payload(cls, payload: object) -> JiraIssueIntakeRoutePayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Jira issue intake routing returned non-object payload")
        route = _require_string(payload, "route", context="Jira issue intake routing payload").lower()
        if route not in {"pm_parent", "engineering_child", "unclear"}:
            raise RuntimeError("Jira issue intake routing returned invalid route")
        reason = _require_string(payload, "reason", context="Jira issue intake routing payload")
        confidence = _require_string(payload, "confidence", context="Jira issue intake routing payload").lower()
        if confidence not in {"high", "medium", "low"}:
            raise RuntimeError("Jira issue intake routing returned invalid confidence")
        return cls(route=route, reason=reason, confidence=confidence)


@dataclass(frozen=True)
class EngineeringClarificationPayload:
    classification: str
    stakeholder_question: str | None
    child_block_note: str
    reason: str

    @classmethod
    def from_payload(cls, payload: object) -> EngineeringClarificationPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Engineering clarification returned non-object payload")
        classification = _require_string(payload, "classification", context="Engineering clarification payload").lower()
        if classification not in {"product_behavior", "technical_implementation"}:
            raise RuntimeError("Engineering clarification returned invalid classification")
        stakeholder_question = _require_optional_string(payload, "stakeholder_question")
        child_block_note = _require_string(payload, "child_block_note", context="Engineering clarification payload")
        reason = _require_string(payload, "reason", context="Engineering clarification payload")
        if classification == "product_behavior" and not stakeholder_question:
            raise RuntimeError("Engineering clarification returned product_behavior without stakeholder_question")
        if classification == "technical_implementation" and stakeholder_question:
            raise RuntimeError(
                "Engineering clarification returned technical_implementation with stakeholder_question"
            )
        return cls(
            classification=classification,
            stakeholder_question=stakeholder_question,
            child_block_note=child_block_note,
            reason=reason,
        )


@dataclass(frozen=True)
class AskIntentPayload:
    mode: str
    summary: str
    command: str | None

    @classmethod
    def from_payload(cls, payload: object) -> AskIntentPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Ask intent returned non-object payload")
        mode = _require_string(payload, "mode", context="Ask intent payload").lower()
        if mode not in {"answer", "command"}:
            raise RuntimeError("Ask intent returned invalid mode")
        summary = _require_string(payload, "summary", context="Ask intent payload")
        command = _require_optional_string(payload, "command")
        if mode == "command":
            if not command:
                raise RuntimeError("Ask intent returned command mode without command")
            if not command.startswith("!"):
                raise RuntimeError("Ask intent returned invalid command")
        elif command:
            raise RuntimeError("Ask intent returned answer mode with command")
        return cls(mode=mode, summary=summary, command=command)


@dataclass(frozen=True)
class RuntimeMessagePayload:
    message: str

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> RuntimeMessagePayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        return cls(message=_require_string(payload, "message", context=context))

    def to_payload(self) -> dict[str, object]:
        return {"message": self.message}


@dataclass(frozen=True)
class RuntimeMessageBriefPayload(RuntimeMessagePayload):
    brief: dict[str, object]

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> RuntimeMessageBriefPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        return cls(
            message=_require_string(payload, "message", context=context),
            brief=_require_dict(payload, "brief", context=context),
        )

    def to_payload(self) -> dict[str, object]:
        return {"message": self.message, "brief": dict(self.brief)}


@dataclass(frozen=True)
class PMInterviewQuestionPayload:
    slot_key: str
    question: str
    examples: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> PMInterviewQuestionPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        return cls(
            slot_key=_require_string(payload, "slot_key", context=context),
            question=_require_string(payload, "question", context=context),
            examples=tuple(_require_string_list(payload, "examples", context=context)),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "slot_key": self.slot_key,
            "question": self.question,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class PMInterviewPlanPayload:
    message: str
    brief: dict[str, object]
    status: str
    ready_to_write: bool
    next_question: PMInterviewQuestionPayload | None

    @classmethod
    def from_payload(cls, payload: object) -> PMInterviewPlanPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("PM interview returned non-object payload")
        brief = _require_dict(payload, "brief", context="PM interview payload")
        status = _require_string(payload, "status", context="PM interview payload").lower()
        allowed_statuses = {"drafting", "question_pending", "researching", "ready_to_write", "pm_completed", "abandoned"}
        if status not in allowed_statuses:
            raise RuntimeError(
                "PM interview payload has invalid status "
                f"{status!r}; expected one of: {', '.join(sorted(allowed_statuses))}"
            )
        ready_to_write = payload.get("ready_to_write")
        if not isinstance(ready_to_write, bool):
            raise RuntimeError("PM interview payload missing ready_to_write")
        raw_next_question = payload.get("next_question")
        if raw_next_question is None:
            next_question = None
        elif isinstance(raw_next_question, str):
            raise RuntimeError("PM interview next_question must be an object")
        else:
            next_question = PMInterviewQuestionPayload.from_payload(
                raw_next_question,
                context="PM interview next_question",
            )
        if not ready_to_write and next_question is None:
            raise RuntimeError("PM interview payload missing next_question for incomplete brief")
        return cls(
            message=_require_string(payload, "message", context="PM interview payload"),
            brief=dict(brief),
            status=status,
            ready_to_write=ready_to_write,
            next_question=next_question,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "message": self.message,
            "brief": dict(self.brief),
            "status": self.status,
            "ready_to_write": self.ready_to_write,
            "next_question": self.next_question.to_payload() if self.next_question is not None else None,
            "next_question_examples": list(self.next_question.examples) if self.next_question is not None else [],
        }


@dataclass(frozen=True)
class ChildTicketSpecPayload:
    summary: str
    capability: str
    delivery: str
    expected_outcome: str
    acceptance_criteria: tuple[str, ...]
    how_to_test: tuple[str, ...]
    done_means: tuple[str, ...]
    dependencies: tuple[str, ...]
    risks: tuple[str, ...]
    labels: tuple[str, ...]

    @classmethod
    def from_payload(
        cls,
        *,
        planning_state: str,
        issue_index: int,
        raw_value: object,
    ) -> ChildTicketSpecPayload:
        if not isinstance(raw_value, dict):
            raise RuntimeError(f"Codex returned invalid {planning_state} child_ticket_specs[{issue_index}] item type")
        return cls(
            summary=_require_planning_text_field(
                planning_state=planning_state,
                issue_index=issue_index,
                field_name="summary",
                raw_value=raw_value.get("summary"),
            ),
            capability=_require_planning_text_field(
                planning_state=planning_state,
                issue_index=issue_index,
                field_name="capability",
                raw_value=raw_value.get("capability"),
            ),
            delivery=_require_planning_text_field(
                planning_state=planning_state,
                issue_index=issue_index,
                field_name="delivery",
                raw_value=raw_value.get("delivery"),
            ),
            expected_outcome=_require_planning_text_field(
                planning_state=planning_state,
                issue_index=issue_index,
                field_name="expected_outcome",
                raw_value=raw_value.get("expected_outcome"),
            ),
            acceptance_criteria=_require_planning_string_tuple_field(
                planning_state=planning_state,
                issue_index=issue_index,
                field_name="acceptance_criteria",
                raw_value=raw_value.get("acceptance_criteria"),
            ),
            how_to_test=_require_planning_string_tuple_field(
                planning_state=planning_state,
                issue_index=issue_index,
                field_name="how_to_test",
                raw_value=raw_value.get("how_to_test"),
            ),
            done_means=_require_planning_string_tuple_field(
                planning_state=planning_state,
                issue_index=issue_index,
                field_name="done_means",
                raw_value=raw_value.get("done_means"),
            ),
            dependencies=_optional_string_tuple(raw_value, "dependencies", context="Child ticket spec payload"),
            risks=_optional_string_tuple(raw_value, "risks", context="Child ticket spec payload"),
            labels=_optional_string_tuple(raw_value, "labels", context="Child ticket spec payload"),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "capability": self.capability,
            "delivery": self.delivery,
            "expected_outcome": self.expected_outcome,
            "acceptance_criteria": list(self.acceptance_criteria),
            "how_to_test": list(self.how_to_test),
            "done_means": list(self.done_means),
            "dependencies": list(self.dependencies),
            "risks": list(self.risks),
            "labels": list(self.labels),
        }


@dataclass(frozen=True)
class TechnicalDecisionOptionPayload:
    option_id: str
    title: str
    description: str
    benefits: tuple[str, ...]
    risks: tuple[str, ...]
    rejected_reason: str | None = None

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> TechnicalDecisionOptionPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        return cls(
            option_id=_require_string(payload, "option_id", context=context),
            title=_require_string(payload, "title", context=context),
            description=_require_string(payload, "description", context=context),
            benefits=_require_string_tuple(payload, "benefits", context=context),
            risks=_require_string_tuple(payload, "risks", context=context),
            rejected_reason=_require_optional_string(payload, "rejected_reason"),
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "option_id": self.option_id,
            "title": self.title,
            "description": self.description,
            "benefits": list(self.benefits),
            "risks": list(self.risks),
        }
        if self.rejected_reason:
            payload["rejected_reason"] = self.rejected_reason
        return payload


@dataclass(frozen=True)
class TechnicalDecisionPayload:
    decision_id: str
    area: str
    question: str
    options: tuple[TechnicalDecisionOptionPayload, ...]
    selected_option_id: str
    rationale: str
    evidence: tuple[str, ...]
    confidence: str
    product_impact: str

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> TechnicalDecisionPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        raw_options = _require_dict_list(payload, "options", context=context)
        options = tuple(
            TechnicalDecisionOptionPayload.from_payload(raw_option, context=f"{context} options[{index}]")
            for index, raw_option in enumerate(raw_options, start=1)
        )
        if not options:
            raise RuntimeError(f"{context} missing options")
        selected_option_id = _require_string(payload, "selected_option_id", context=context)
        if selected_option_id not in {option.option_id for option in options}:
            raise RuntimeError(f"{context} selected_option_id does not match an option_id")
        confidence = _require_string(payload, "confidence", context=context).lower()
        if confidence not in {"high", "medium", "low"}:
            raise RuntimeError(f"{context} has invalid confidence")
        product_impact = _require_string(payload, "product_impact", context=context).lower()
        if product_impact not in {
            "none",
            "product_behavior",
            "scope",
            "acceptance_criteria",
            "compliance",
            "rollout",
            "user_visible_semantics",
        }:
            raise RuntimeError(f"{context} has invalid product_impact")
        return cls(
            decision_id=_require_string(payload, "decision_id", context=context),
            area=_require_string(payload, "area", context=context),
            question=_require_string(payload, "question", context=context),
            options=options,
            selected_option_id=selected_option_id,
            rationale=_require_string(payload, "rationale", context=context),
            evidence=_require_string_tuple(payload, "evidence", context=context),
            confidence=confidence,
            product_impact=product_impact,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "area": self.area,
            "question": self.question,
            "options": [option.to_payload() for option in self.options],
            "selected_option_id": self.selected_option_id,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
            "confidence": self.confidence,
            "product_impact": self.product_impact,
        }


@dataclass(frozen=True)
class PMDecisionRequestPayload:
    request_id: str
    question: str
    why_it_matters: str
    related_decision_ids: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> PMDecisionRequestPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        related_decision_ids = _require_string_tuple(payload, "related_decision_ids", context=context)
        if not related_decision_ids:
            raise RuntimeError(f"{context} missing related_decision_ids")
        return cls(
            request_id=_require_string(payload, "request_id", context=context),
            question=_require_string(payload, "question", context=context),
            why_it_matters=_require_string(payload, "why_it_matters", context=context),
            related_decision_ids=related_decision_ids,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "question": self.question,
            "why_it_matters": self.why_it_matters,
            "related_decision_ids": list(self.related_decision_ids),
        }


@dataclass(frozen=True)
class StakeholderEscalationPayload:
    escalation_id: str
    question: str
    why_it_matters: str
    business_impact_area: str
    source_pm_decision_request_ids: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> StakeholderEscalationPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        business_impact_area = _require_string(payload, "business_impact_area", context=context).lower()
        if business_impact_area not in {
            "business_outcome",
            "risk_compliance",
            "timeline_delivery",
            "team_impact",
            "customer_business_impact",
        }:
            raise RuntimeError(f"{context} has invalid business_impact_area")
        request_ids = _require_string_tuple(payload, "source_pm_decision_request_ids", context=context)
        if not request_ids:
            raise RuntimeError(f"{context} missing source_pm_decision_request_ids")
        return cls(
            escalation_id=_require_string(payload, "escalation_id", context=context),
            question=_require_string(payload, "question", context=context),
            why_it_matters=_require_string(payload, "why_it_matters", context=context),
            business_impact_area=business_impact_area,
            source_pm_decision_request_ids=request_ids,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "escalation_id": self.escalation_id,
            "question": self.question,
            "why_it_matters": self.why_it_matters,
            "business_impact_area": self.business_impact_area,
            "source_pm_decision_request_ids": list(self.source_pm_decision_request_ids),
        }

    def to_clarification_question(self) -> ClarificationQuestion:
        return ClarificationQuestion(
            question=self.question,
            why_it_matters=self.why_it_matters,
            source_ref="stakeholder_escalation",
        )


@dataclass(frozen=True)
class PMDecisionResolutionPayload:
    request_id: str
    answer: str
    rationale: str
    evidence: tuple[str, ...]
    planning_context_delta: dict[str, object]

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> PMDecisionResolutionPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        raw_delta = payload.get("planning_context_delta")
        if raw_delta is None:
            raw_delta = {}
        if not isinstance(raw_delta, dict):
            raise RuntimeError(f"{context} has invalid planning_context_delta")
        return cls(
            request_id=_require_string(payload, "request_id", context=context),
            answer=_require_string(payload, "answer", context=context),
            rationale=_require_string(payload, "rationale", context=context),
            evidence=_require_string_tuple(payload, "evidence", context=context),
            planning_context_delta=dict(raw_delta),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "answer": self.answer,
            "rationale": self.rationale,
            "evidence": list(self.evidence),
            "planning_context_delta": dict(self.planning_context_delta),
        }


@dataclass(frozen=True)
class PMDecisionResolutionSetPayload:
    resolved_decisions: tuple[PMDecisionResolutionPayload, ...]
    stakeholder_escalations: tuple[StakeholderEscalationPayload, ...]
    updated_planning_context: dict[str, object]

    @classmethod
    def from_payload(
        cls,
        payload: object,
        *,
        expected_request_ids: tuple[str, ...],
        context: str,
    ) -> PMDecisionResolutionSetPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        known_request_ids = set(expected_request_ids)
        resolutions = tuple(
            PMDecisionResolutionPayload.from_payload(raw_resolution, context=f"{context} resolved_decisions[{index}]")
            for index, raw_resolution in enumerate(
                _require_dict_list(payload, "resolved_decisions", context=context),
                start=1,
            )
        )
        stakeholder_escalations = tuple(
            StakeholderEscalationPayload.from_payload(
                raw_escalation,
                context=f"{context} stakeholder_escalations[{index}]",
            )
            for index, raw_escalation in enumerate(
                _require_dict_list(payload, "stakeholder_escalations", context=context),
                start=1,
            )
        )
        resolved_request_ids = tuple(resolution.request_id for resolution in resolutions)
        duplicate_resolved_ids = {
            request_id for request_id in resolved_request_ids if resolved_request_ids.count(request_id) > 1
        }
        if duplicate_resolved_ids:
            raise RuntimeError(f"{context} has duplicate resolved_decisions request_id")
        unknown_resolved_ids = tuple(request_id for request_id in resolved_request_ids if request_id not in known_request_ids)
        if unknown_resolved_ids:
            raise RuntimeError(f"{context} resolved_decisions references unknown request_id {', '.join(unknown_resolved_ids)}")
        for index, escalation in enumerate(stakeholder_escalations, start=1):
            unknown_ids = tuple(
                request_id
                for request_id in escalation.source_pm_decision_request_ids
                if request_id not in known_request_ids
            )
            if unknown_ids:
                raise RuntimeError(
                    f"{context} stakeholder_escalations[{index}] references unknown request_id {', '.join(unknown_ids)}"
                )
        raw_updated_context = payload.get("updated_planning_context")
        if raw_updated_context is None:
            raw_updated_context = {}
        if not isinstance(raw_updated_context, dict):
            raise RuntimeError(f"{context} has invalid updated_planning_context")
        return cls(
            resolved_decisions=resolutions,
            stakeholder_escalations=stakeholder_escalations,
            updated_planning_context=dict(raw_updated_context),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "resolved_decisions": [resolution.to_payload() for resolution in self.resolved_decisions],
            "stakeholder_escalations": [escalation.to_payload() for escalation in self.stakeholder_escalations],
            "updated_planning_context": dict(self.updated_planning_context),
        }


@dataclass(frozen=True)
class PlanningStageOutputPayload:
    __test__ = False
    planning_state: str
    persona_id: str
    role_label: str
    findings: tuple[str, ...]
    recommendations: tuple[str, ...]
    technical_decisions: tuple[TechnicalDecisionPayload, ...]
    pm_decision_requests: tuple[PMDecisionRequestPayload, ...]
    acceptance_impacts: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.pm_decision_requests)

    @classmethod
    def _base_from_payload(
        cls,
        *,
        planning_state: str,
        persona_id: str,
        role_label: str,
        payload: object,
    ) -> dict[str, object]:
        if not isinstance(payload, dict):
            raise RuntimeError(f"Codex did not return a {planning_state} JSON object")
        raw_technical_decisions = payload.get("technical_decisions")
        if not isinstance(raw_technical_decisions, list):
            raise RuntimeError(f"{planning_state} missing technical_decisions")
        if "product_escalations" in payload:
            raise RuntimeError(f"{planning_state} uses removed product_escalations contract")
        raw_pm_decision_requests = payload.get("pm_decision_requests")
        if not isinstance(raw_pm_decision_requests, list):
            raise RuntimeError(f"{planning_state} missing pm_decision_requests")
        technical_decisions = tuple(
            TechnicalDecisionPayload.from_payload(
                raw_decision,
                context=f"{planning_state} technical_decisions[{index}]",
            )
            for index, raw_decision in enumerate(raw_technical_decisions, start=1)
        )
        decision_ids = tuple(decision.decision_id for decision in technical_decisions)
        duplicate_decision_ids = {decision_id for decision_id in decision_ids if decision_ids.count(decision_id) > 1}
        if duplicate_decision_ids:
            raise RuntimeError(
                f"{planning_state} technical_decisions has duplicate decision_id "
                f"{', '.join(sorted(duplicate_decision_ids))}"
            )
        known_decision_ids = set(decision_ids)
        pm_decision_requests = tuple(
            PMDecisionRequestPayload.from_payload(
                raw_request,
                context=f"{planning_state} pm_decision_requests[{index}]",
            )
            for index, raw_request in enumerate(raw_pm_decision_requests, start=1)
        )
        request_ids = tuple(request.request_id for request in pm_decision_requests)
        duplicate_request_ids = {request_id for request_id in request_ids if request_ids.count(request_id) > 1}
        if duplicate_request_ids:
            raise RuntimeError(
                f"{planning_state} pm_decision_requests has duplicate request_id "
                f"{', '.join(sorted(duplicate_request_ids))}"
            )
        for index, request in enumerate(pm_decision_requests, start=1):
            unknown_ids = tuple(
                decision_id for decision_id in request.related_decision_ids if decision_id not in known_decision_ids
            )
            if unknown_ids:
                raise RuntimeError(
                    f"{planning_state} pm_decision_requests[{index}] references unknown related_decision_ids "
                    f"{', '.join(unknown_ids)}"
                )
        return {
            "planning_state": planning_state,
            "persona_id": persona_id,
            "role_label": role_label,
            "findings": _require_string_tuple(payload, "findings", context=f"{planning_state} payload"),
            "recommendations": _require_string_tuple(payload, "recommendations", context=f"{planning_state} payload"),
            "technical_decisions": technical_decisions,
            "pm_decision_requests": pm_decision_requests,
            "acceptance_impacts": _require_string_tuple(
                payload,
                "acceptance_impacts",
                context=f"{planning_state} payload",
            ),
        }

    def _base_payload(self) -> dict[str, object]:
        return {
            "planning_state": self.planning_state,
            "persona_id": self.persona_id,
            "role_label": self.role_label,
            "blocked": self.blocked,
            "findings": list(self.findings),
            "recommendations": list(self.recommendations),
            "technical_decisions": [decision.to_payload() for decision in self.technical_decisions],
            "pm_decision_requests": [request.to_payload() for request in self.pm_decision_requests],
            "acceptance_impacts": list(self.acceptance_impacts),
        }

    def to_payload(self) -> dict[str, object]:
        return self._base_payload()


@dataclass(frozen=True)
class ArchitectStageOutputPayload(PlanningStageOutputPayload):
    __test__ = False
    required_tasks: tuple[str, ...]
    child_ticket_specs: tuple[ChildTicketSpecPayload, ...]
    mermaid_diagram: str | None = None

    @classmethod
    def from_payload(
        cls,
        *,
        planning_state: str,
        persona_id: str,
        role_label: str,
        payload: object,
    ) -> ArchitectStageOutputPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"Codex did not return a {planning_state} JSON object")
        base = cls._base_from_payload(
            planning_state=planning_state,
            persona_id=persona_id,
            role_label=role_label,
            payload=payload,
        )
        raw_child_specs = payload.get("child_ticket_specs")
        if not isinstance(raw_child_specs, list):
            raise RuntimeError(f"{planning_state} missing child_ticket_specs")
        child_ticket_specs = tuple(
            ChildTicketSpecPayload.from_payload(
                planning_state=planning_state,
                issue_index=issue_index,
                raw_value=raw_item,
            )
            for issue_index, raw_item in enumerate(raw_child_specs, start=1)
        )
        return cls(
            **base,
            required_tasks=_require_string_tuple(payload, "required_tasks", context=f"{planning_state} payload"),
            child_ticket_specs=child_ticket_specs,
            mermaid_diagram=_normalized_optional_text(payload.get("mermaid_diagram")),
        )

    def to_payload(self) -> dict[str, object]:
        payload = self._base_payload()
        payload["required_tasks"] = list(self.required_tasks)
        payload["child_ticket_specs"] = [spec.to_payload() for spec in self.child_ticket_specs]
        if self.mermaid_diagram:
            payload["mermaid_diagram"] = self.mermaid_diagram
        return payload


@dataclass(frozen=True)
class SecurityStageOutputPayload(PlanningStageOutputPayload):
    __test__ = False
    required_tasks: tuple[str, ...]

    @classmethod
    def from_payload(
        cls,
        *,
        planning_state: str,
        persona_id: str,
        role_label: str,
        payload: object,
    ) -> SecurityStageOutputPayload:
        base = cls._base_from_payload(
            planning_state=planning_state,
            persona_id=persona_id,
            role_label=role_label,
            payload=payload,
        )
        if not isinstance(payload, dict):
            raise RuntimeError(f"Codex did not return a {planning_state} JSON object")
        return cls(
            **base,
            required_tasks=_require_string_tuple(payload, "required_tasks", context=f"{planning_state} payload"),
        )

    def to_payload(self) -> dict[str, object]:
        payload = self._base_payload()
        payload["required_tasks"] = list(self.required_tasks)
        return payload


@dataclass(frozen=True)
class TestingStageOutputPayload(SecurityStageOutputPayload):
    __test__ = False


@dataclass(frozen=True)
class PMToolCallPayload:
    tool: str
    arguments: dict[str, object]

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> PMToolCallPayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} has invalid tool_calls item")
        tool = _require_string(payload, "tool", context=f"{context} tool_calls item")
        arguments = payload.get("arguments")
        if not isinstance(arguments, dict):
            raise RuntimeError(f"{context} has invalid tool_calls item")
        return cls(tool=tool, arguments=dict(arguments))

    def to_payload(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "arguments": dict(self.arguments),
        }


@dataclass(frozen=True)
class DesignPlanningPayload:
    selected_plugin_id: str
    decision_state: str
    stage_artifacts: dict[str, object]
    stage_open_questions: tuple[str, ...]
    tool_calls: tuple[PMToolCallPayload, ...]
    message: str

    @classmethod
    def from_payload(cls, payload: object) -> DesignPlanningPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Design planning returned non-object payload")
        selected_plugin_id = _require_string(payload, "selected_plugin_id", context="Design planning payload").lower()
        decision_state = _require_string(payload, "decision_state", context="Design planning payload").lower()
        if decision_state not in {"approved", "revisions_required", "pending"}:
            raise RuntimeError("Design planning returned invalid decision_state")
        stage_artifacts = _require_dict(payload, "stage_artifacts", context="Design planning payload")
        stage_open_questions = tuple(
            _require_string_list(payload, "stage_open_questions", context="Design planning payload")
        )
        raw_tool_calls = payload.get("tool_calls")
        if not isinstance(raw_tool_calls, list):
            raise RuntimeError("Design planning payload missing tool_calls")
        tool_calls = tuple(
            PMToolCallPayload.from_payload(raw_item, context="Design planning payload")
            for raw_item in raw_tool_calls
        )
        message = _require_string(payload, "message", context="Design planning payload")
        return cls(
            selected_plugin_id=selected_plugin_id,
            decision_state=decision_state,
            stage_artifacts=stage_artifacts,
            stage_open_questions=stage_open_questions,
            tool_calls=tool_calls,
            message=message,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "selected_plugin_id": self.selected_plugin_id,
            "decision_state": self.decision_state,
            "stage_artifacts": dict(self.stage_artifacts),
            "stage_open_questions": list(self.stage_open_questions),
            "tool_calls": [item.to_payload() for item in self.tool_calls],
            "message": self.message,
        }


def _require_seed_optional_text(payload: dict[str, object], key: str, *, context: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError(f"{context} has invalid {key}")
    return " ".join(value.split()) or None


def _require_seed_list(payload: dict[str, object], key: str, *, context: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise RuntimeError(f"{context} missing {key}")
    return _normalized_string_tuple(value)


@dataclass(frozen=True)
class ParentSeedIssuePayload:
    summary: str
    issue_type: str
    objective: str
    user_value: str
    recommendation: str
    scope_in: tuple[str, ...]
    scope_out: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    ui_references: tuple[str, ...]
    success_outcomes: tuple[str, ...]
    dependencies: tuple[str, ...]
    risks: tuple[str, ...]
    open_questions: tuple[str, ...]
    labels: tuple[str, ...]
    issue_key: str | None = None

    @classmethod
    def from_payload(cls, payload: object, *, context: str) -> ParentSeedIssuePayload:
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        return cls(
            summary=_require_string(payload, "summary", context=context),
            issue_type=_require_string(payload, "issue_type", context=context),
            objective=_require_string(payload, "objective", context=context),
            user_value=_require_string(payload, "user_value", context=context),
            recommendation=_require_string(payload, "recommendation", context=context),
            scope_in=_require_seed_list(payload, "scope_in", context=context),
            scope_out=_require_seed_list(payload, "scope_out", context=context),
            acceptance_criteria=_require_seed_list(payload, "acceptance_criteria", context=context),
            ui_references=_require_seed_list(payload, "ui_references", context=context),
            success_outcomes=_require_seed_list(payload, "success_outcomes", context=context),
            dependencies=_require_seed_list(payload, "dependencies", context=context),
            risks=_require_seed_list(payload, "risks", context=context),
            open_questions=_require_seed_list(payload, "open_questions", context=context),
            labels=_require_seed_list(payload, "labels", context=context),
            issue_key=_require_seed_optional_text(payload, "issue_key", context=context),
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "summary": self.summary,
            "issue_type": self.issue_type,
            "objective": self.objective,
            "user_value": self.user_value,
            "recommendation": self.recommendation,
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "acceptance_criteria": list(self.acceptance_criteria),
            "ui_references": list(self.ui_references),
            "success_outcomes": list(self.success_outcomes),
            "dependencies": list(self.dependencies),
            "risks": list(self.risks),
            "open_questions": list(self.open_questions),
            "labels": list(self.labels),
        }
        if self.issue_key:
            payload["issue_key"] = self.issue_key
        return payload


@dataclass(frozen=True)
class EngineeringSeedChildPayload:
    summary: str
    issue_type: str
    capability: str
    delivery: str
    expected_outcome: str
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...]
    risks: tuple[str, ...]
    how_to_test: tuple[str, ...]
    done_means: tuple[str, ...]
    labels: tuple[str, ...]
    issue_key: str | None = None

    @classmethod
    def from_payload(cls, payload: object, *, issue_index: int) -> EngineeringSeedChildPayload:
        context = f"Issue seeding engineering_children[{issue_index}]"
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context} must be an object")
        return cls(
            summary=_require_string(payload, "summary", context=context),
            issue_type=_require_string(payload, "issue_type", context=context),
            capability=_require_string(payload, "capability", context=context),
            delivery=_require_string(payload, "delivery", context=context),
            expected_outcome=_require_string(payload, "expected_outcome", context=context),
            acceptance_criteria=_require_seed_list(payload, "acceptance_criteria", context=context),
            dependencies=_require_seed_list(payload, "dependencies", context=context),
            risks=_require_seed_list(payload, "risks", context=context),
            how_to_test=_require_seed_list(payload, "how_to_test", context=context),
            done_means=_require_seed_list(payload, "done_means", context=context),
            labels=_require_seed_list(payload, "labels", context=context),
            issue_key=_require_seed_optional_text(payload, "issue_key", context=context),
        )

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "summary": self.summary,
            "issue_type": self.issue_type,
            "capability": self.capability,
            "delivery": self.delivery,
            "expected_outcome": self.expected_outcome,
            "acceptance_criteria": list(self.acceptance_criteria),
            "dependencies": list(self.dependencies),
            "risks": list(self.risks),
            "how_to_test": list(self.how_to_test),
            "done_means": list(self.done_means),
            "labels": list(self.labels),
        }
        if self.issue_key:
            payload["issue_key"] = self.issue_key
        return payload


@dataclass(frozen=True)
class EngineeringSeedPlanPayload:
    project_key: str
    parent_issue: ParentSeedIssuePayload
    engineering_children: tuple[EngineeringSeedChildPayload, ...]
    questions: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object) -> EngineeringSeedPlanPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("Issue seeding returned non-object payload")
        raw_children = payload.get("engineering_children")
        if not isinstance(raw_children, list):
            raise RuntimeError("Issue seeding payload missing engineering_children")
        return cls(
            project_key=_require_string(payload, "project_key", context="Issue seeding payload").upper(),
            parent_issue=ParentSeedIssuePayload.from_payload(
                payload.get("parent_issue"),
                context="Issue seeding parent_issue",
            ),
            engineering_children=tuple(
                EngineeringSeedChildPayload.from_payload(raw_child, issue_index=issue_index)
                for issue_index, raw_child in enumerate(raw_children, start=1)
            ),
            questions=tuple(_require_string_list(payload, "questions", context="Issue seeding payload")),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "project_key": self.project_key,
            "parent_issue": self.parent_issue.to_payload(),
            "engineering_children": [item.to_payload() for item in self.engineering_children],
            "questions": list(self.questions),
        }


@dataclass(frozen=True)
class PmParentSeedPlanPayload:
    project_key: str
    issues: tuple[ParentSeedIssuePayload, ...]
    questions: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: object) -> PmParentSeedPlanPayload:
        if not isinstance(payload, dict):
            raise RuntimeError("PM parent seeding returned non-object payload")
        raw_issues = payload.get("issues")
        if not isinstance(raw_issues, list):
            raise RuntimeError("PM parent seeding payload missing issues")
        return cls(
            project_key=_require_string(payload, "project_key", context="PM parent seeding payload").upper(),
            issues=tuple(
                ParentSeedIssuePayload.from_payload(
                    raw_issue,
                    context=f"PM parent seeding issues[{issue_index}]",
                )
                for issue_index, raw_issue in enumerate(raw_issues, start=1)
            ),
            questions=tuple(_require_string_list(payload, "questions", context="PM parent seeding payload")),
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "project_key": self.project_key,
            "issues": [item.to_payload() for item in self.issues],
            "questions": list(self.questions),
        }
