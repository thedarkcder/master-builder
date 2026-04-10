from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_types import (
    DecisionClassification,
    DecisionQuestionKind,
    IngressDecision,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult

QUESTION_KIND_DECISION_GATE = DecisionQuestionKind.DECISION_GATE.value
QUESTION_KIND_GTD = DecisionQuestionKind.GTD.value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


@dataclass(frozen=True)
class DecisionQuestionSnapshot:
    question_id: str
    kind: DecisionQuestionKind
    text: str
    status: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class PrecheckSnapshot:
    outcome: str
    ready_label: str | None
    ready_label_present: bool
    required_worker_capability: str
    required_worker_label: str
    required_worker_label_present: bool
    decision_gate: DecisionGateResult
    gtd: GoodToDoValidationResult

    @classmethod
    def from_precheck(cls, pre_check: PreRunCheckResult) -> PrecheckSnapshot:
        return cls(
            outcome=pre_check.outcome,
            ready_label=pre_check.ready_label,
            ready_label_present=pre_check.ready_label_present,
            required_worker_capability=pre_check.required_worker_capability,
            required_worker_label=pre_check.required_worker_label,
            required_worker_label_present=pre_check.required_worker_label_present,
            decision_gate=pre_check.decision_gate,
            gtd=pre_check.gtd,
        )

    def to_precheck(self) -> PreRunCheckResult:
        return PreRunCheckResult(
            outcome=self.outcome,
            ready_label=self.ready_label,
            ready_label_present=self.ready_label_present,
            required_worker_capability=self.required_worker_capability,
            required_worker_label=self.required_worker_label,
            required_worker_label_present=self.required_worker_label_present,
            decision_gate=self.decision_gate,
            gtd=self.gtd,
        )

    @classmethod
    def load(cls, payload: object) -> PrecheckSnapshot | None:
        if not isinstance(payload, dict):
            return None
        decision_gate_payload = payload.get("decision_gate")
        gtd_payload = payload.get("gtd")
        if not isinstance(decision_gate_payload, dict) or not isinstance(gtd_payload, dict):
            return None
        return cls(
            outcome=str(payload.get("outcome") or "").strip(),
            ready_label=str(payload.get("ready_label") or "").strip() or None,
            ready_label_present=bool(payload.get("ready_label_present", False)),
            required_worker_capability=str(payload.get("required_worker_capability") or "").strip(),
            required_worker_label=str(payload.get("required_worker_label") or "").strip(),
            required_worker_label_present=bool(payload.get("required_worker_label_present", False)),
            decision_gate=DecisionGateResult(
                triggered=bool(decision_gate_payload.get("triggered")),
                reason=str(decision_gate_payload.get("reason") or "").strip(),
                missing_sections=_string_tuple(decision_gate_payload.get("missing_sections")),
                questions=_string_tuple(decision_gate_payload.get("questions")),
                recommendation=str(decision_gate_payload.get("recommendation") or "").strip(),
                tags=_string_tuple(decision_gate_payload.get("tags")),
            ),
            gtd=GoodToDoValidationResult(
                valid=bool(gtd_payload.get("valid")),
                missing_criteria=_string_tuple(gtd_payload.get("missing_criteria")),
                clarification_questions=_string_tuple(gtd_payload.get("clarification_questions")),
            ),
        )

    def dump(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "ready_label": self.ready_label,
            "ready_label_present": self.ready_label_present,
            "required_worker_capability": self.required_worker_capability,
            "required_worker_label": self.required_worker_label,
            "required_worker_label_present": self.required_worker_label_present,
            "decision_gate": {
                "triggered": self.decision_gate.triggered,
                "reason": self.decision_gate.reason,
                "missing_sections": list(self.decision_gate.missing_sections),
                "questions": list(self.decision_gate.questions),
                "recommendation": self.decision_gate.recommendation,
                "tags": list(self.decision_gate.tags),
            },
            "gtd": {
                "valid": self.gtd.valid,
                "missing_criteria": list(self.gtd.missing_criteria),
                "clarification_questions": list(self.gtd.clarification_questions),
            },
        }


@dataclass(frozen=True)
class DecisionResultSnapshot:
    classification: DecisionClassification
    issue_labels: tuple[str, ...]
    missing_slots: tuple[str, ...]
    auto_resolved_slots: tuple[str, ...]
    block_reason: str | None
    guidance: str | None
    policy_error: str | None
    pre_check: PrecheckSnapshot | None

    @classmethod
    def from_decision(
        cls,
        *,
        decision: IngressDecision,
        classification: str,
        issue_labels: list[str],
        missing_slots: list[str],
        auto_resolved_slots: list[str],
    ) -> DecisionResultSnapshot:
        return cls(
            classification=DecisionClassification.parse(classification),
            issue_labels=tuple(str(label).strip() for label in issue_labels if str(label).strip()),
            missing_slots=tuple(str(slot).strip() for slot in missing_slots if str(slot).strip()),
            auto_resolved_slots=tuple(
                str(slot).strip() for slot in auto_resolved_slots if str(slot).strip()
            ),
            block_reason=decision.block_reason,
            guidance=decision.guidance,
            policy_error=decision.policy_error,
            pre_check=(
                PrecheckSnapshot.from_precheck(decision.pre_check)
                if isinstance(decision.pre_check, PreRunCheckResult)
                else None
            ),
        )

    @classmethod
    def load(cls, payload: object) -> DecisionResultSnapshot | None:
        if not isinstance(payload, dict):
            return None
        return cls(
            classification=DecisionClassification.parse(payload.get("classification")),
            issue_labels=tuple(str(label).strip() for label in payload.get("issue_labels", []) if str(label).strip()),
            missing_slots=tuple(str(slot).strip() for slot in payload.get("missing_slots", []) if str(slot).strip()),
            auto_resolved_slots=tuple(
                str(slot).strip() for slot in payload.get("auto_resolved_slots", []) if str(slot).strip()
            ),
            block_reason=str(payload.get("block_reason") or "").strip() or None,
            guidance=str(payload.get("guidance") or "").strip() or None,
            policy_error=str(payload.get("policy_error") or "").strip() or None,
            pre_check=PrecheckSnapshot.load(payload.get("pre_check")),
        )

    def dump(self) -> dict[str, object]:
        return {
            "classification": self.classification.value,
            "issue_labels": list(self.issue_labels),
            "missing_slots": list(self.missing_slots),
            "auto_resolved_slots": list(self.auto_resolved_slots),
            "block_reason": self.block_reason,
            "guidance": self.guidance,
            "policy_error": self.policy_error,
            "pre_check": self.pre_check.dump() if self.pre_check is not None else None,
        }


def stable_short_hash(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:10]


def build_question_set(*, pre_check: object, classification: str) -> list[dict]:
    if pre_check is None:
        return []
    items: list[dict[str, str]] = []
    parsed_classification = DecisionClassification.parse(classification)
    if parsed_classification.includes_decision_gate:
        decision_gate = getattr(pre_check, "decision_gate", None)
        questions = getattr(decision_gate, "questions", ()) if decision_gate is not None else ()
        for question in questions:
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"dg_{stable_short_hash(text)}",
                    "kind": DecisionQuestionKind.DECISION_GATE.value,
                    "text": text,
                }
            )
    if parsed_classification.includes_gtd:
        for question in getattr(pre_check, "gtd_clarification_questions", ()):
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"gtd_{stable_short_hash(text)}",
                    "kind": DecisionQuestionKind.GTD.value,
                    "text": text,
                }
            )
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        deduped.append(item)
        seen.add(item_id)
    return deduped


def apply_frozen_cycle_questions(
    *,
    pre_check: object,
    cycle_question_set: list[dict[str, object]],
    unresolved_question_ids: list[str],
    cycle_reason: str | None,
    classification: DecisionClassification | object,
) -> object:
    parsed_classification = DecisionClassification.parse(classification)
    unresolved_ids = {str(question_id).strip() for question_id in unresolved_question_ids if str(question_id).strip()}
    decision_gate_questions = [
        str(item.get("text") or "").strip()
        for item in cycle_question_set
        if (
            DecisionQuestionKind.parse(item.get("kind")) is DecisionQuestionKind.DECISION_GATE
            and str(item.get("text") or "").strip()
            and (
                not str(item.get("id") or "").strip()
                or str(item.get("id") or "").strip() in unresolved_ids
            )
        )
    ]
    gtd_questions = [
        str(item.get("text") or "").strip()
        for item in cycle_question_set
        if (
            DecisionQuestionKind.parse(item.get("kind")) is DecisionQuestionKind.GTD
            and str(item.get("text") or "").strip()
            and (
                not str(item.get("id") or "").strip()
                or str(item.get("id") or "").strip() in unresolved_ids
            )
        )
    ]
    resolved = pre_check
    decision_gate = getattr(resolved, "decision_gate", None)
    gtd = getattr(resolved, "gtd", None)
    if parsed_classification.includes_decision_gate and decision_gate is not None:
        next_decision_gate = decision_gate
        if cycle_reason:
            next_decision_gate = replace(next_decision_gate, reason=cycle_reason)
        next_decision_gate = replace(next_decision_gate, questions=tuple(decision_gate_questions))
        resolved = replace(resolved, decision_gate=next_decision_gate)
    if parsed_classification.includes_gtd and gtd is not None:
        resolved = replace(resolved, gtd=replace(gtd, clarification_questions=tuple(gtd_questions)))
    return resolved
