from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.knowledge_base import SlotResolution
from orchestrator.core.pre_run_check import PreRunCheckResult, evaluate_pre_run_check
from orchestrator.core.precheck_decision import precheck_missing_slots
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_types import (
    DecisionSource,
    DecisionEventInput,
    DecisionLabelAction,
    IngressDecision,
    blocking_reason_for_precheck,
    resolve_execution_gate_state,
)
from orchestrator.storage.models import DecisionCase, DecisionCycle

QUESTION_KIND_DG = "decision_gate"
QUESTION_KIND_GTD = "gtd"
BLOCKED_CLASSIFICATIONS = {"decision_gate", "gtd", "both"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvaluationResult:
    decision: IngressDecision
    issue_labels: list[str]


def evaluate_with_labels(
    *,
    session: Session,
    tenant,
    project,
    source: DecisionSource,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    recorded_answers: list[dict[str, str]] | None,
    issue_labels: list[str] | None,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn: Callable[..., Any],
    evaluate_ingress_precheck_fn: Callable[..., IngressDecision],
    oauth_context: Any | None = None,
    evaluate_pre_run_check_fn: Callable[..., object] = evaluate_pre_run_check,
):
    from orchestrator.core.label_action_service import apply_issue_label_actions

    decision = evaluate_ingress_precheck_fn(
        source=source,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else None,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        recorded_answers=recorded_answers,
        issue_labels=issue_labels,
        ready_label=(tenant.jira_config or {}).get("ready_label"),
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )
    normalized_labels = [str(label).strip() for label in (issue_labels or []) if str(label).strip()]
    if decision.pre_check is None:
        return EvaluationResult(decision=decision, issue_labels=normalized_labels)

    apply_result = apply_issue_label_actions(
        session=session,
        tenant=tenant,
        project_policy_overrides=project.policy_overrides if project is not None else {},
        issue_key=issue_key,
        existing_labels=normalized_labels,
        actions=decision.label_actions,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        oauth_context=oauth_context,
        logger=logger,
    )
    label_set = {label.casefold() for label in normalized_labels}
    for label in apply_result.applied_labels:
        if label.casefold() in label_set:
            continue
        normalized_labels.append(label)
        label_set.add(label.casefold())
    resolved = decision.with_applied_labels(list(apply_result.applied_labels))
    return EvaluationResult(decision=resolved, issue_labels=normalized_labels)


def derive_label_actions(pre_check: object) -> tuple[DecisionLabelAction, ...]:
    actions: list[DecisionLabelAction] = []
    ready_label_missing = bool(getattr(pre_check, "ready_label_missing", False))
    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip()
    outcome = str(getattr(pre_check, "outcome", "") or "").strip().lower()
    if ready_label_missing and ready_label and outcome == "missing_ready_label":
        actions.append(
            DecisionLabelAction(
                label=ready_label,
                action="add",
                reason="ready_label_missing",
            )
        )
    required_worker_label = str(getattr(pre_check, "required_worker_label", "") or "").strip()
    required_worker_label_present = bool(getattr(pre_check, "required_worker_label_present", True))
    if not required_worker_label_present and required_worker_label:
        actions.append(
            DecisionLabelAction(
                label=required_worker_label,
                action="add",
                reason="required_worker_label_missing",
            )
        )
    deduped: list[DecisionLabelAction] = []
    seen: set[str] = set()
    for action in actions:
        key = action.label.casefold()
        if key in seen:
            continue
        deduped.append(action)
        seen.add(key)
    return tuple(deduped)


def decision_result_for_duplicate_event(
    *,
    session,
    existing_event,
    decision_result_type,
    decision_case_type,
    decision_cycle_type,
    decision_effect_outbox_type,
) -> object:
    payload = existing_event.payload_json if isinstance(existing_event.payload_json, dict) else {}
    snapshot = payload.get("result_snapshot") if isinstance(payload.get("result_snapshot"), dict) else {}
    issue_labels = [
        str(label).strip()
        for label in snapshot.get("issue_labels", payload.get("issue_labels", []))
        if str(label).strip()
    ]
    classification = str(snapshot.get("classification") or "").strip() or "clear"
    missing_slots = string_tuple(snapshot.get("missing_slots"))
    auto_resolved_slots = string_tuple(snapshot.get("auto_resolved_slots"))

    case = session.get(decision_case_type, existing_event.case_id)
    if case is None:
        raise RuntimeError(f"Decision event {existing_event.event_id} references missing case {existing_event.case_id}")
    cycle = session.get(decision_cycle_type, existing_event.cycle_id) if existing_event.cycle_id else None
    decision = decision_from_snapshot(
        snapshot=snapshot,
        source=str(existing_event.source or "jira_webhook"),
        classification=classification,
        cycle=cycle,
        case=case,
    )
    if classification == "clear":
        normalize_clear_case_snapshot(
            case=case,
            source=str(existing_event.source or "jira_webhook"),
            occurred_at=getattr(existing_event, "occurred_at", None) or getattr(existing_event, "created_at", None),
        )
    from sqlalchemy import select

    outbox_effect_ids = tuple(
        str(effect.effect_id)
        for effect in session.execute(
            select(decision_effect_outbox_type).where(
                decision_effect_outbox_type.case_id == case.case_id,
                decision_effect_outbox_type.cycle_id == (cycle.cycle_id if cycle is not None else None),
            )
        ).scalars()
    )
    execution_gate = resolve_execution_gate_state(
        decision=decision,
        classification=classification,
    )
    return decision_result_type(
        decision=decision,
        issue_labels=issue_labels,
        classification=classification,
        missing_slots=list(missing_slots),
        auto_resolved_slots=list(auto_resolved_slots),
        case_id=case.case_id,
        case_state=case.state,
        cycle_id=cycle.cycle_id if cycle is not None else None,
        outbox_effect_ids=outbox_effect_ids,
        duplicate_event=True,
        execution_gate=execution_gate,
    )


def decision_from_snapshot(
    *,
    snapshot: dict[str, Any],
    source: str,
    classification: str,
    cycle: DecisionCycle | None,
    case: DecisionCase,
) -> IngressDecision:
    pre_check = deserialize_precheck_result(snapshot.get("pre_check"))
    if pre_check is not None and cycle is not None and cycle.status == "open":
        pre_check = apply_frozen_cycle_to_precheck(
            pre_check=pre_check,
            cycle=cycle,
            classification=classification,
        )
    if classification == "clear":
        normalized_pre_check = pre_check
        if normalized_pre_check is None:
            ready_label = str(case.ready_label or "").strip() or None
            ready_label_present = bool(case.ready_label_present)
            outcome = "missing_ready_label" if ready_label and not ready_label_present else "ready_for_agent"
            normalized_pre_check = PreRunCheckResult(
                outcome=outcome,
                ready_label=ready_label,
                ready_label_present=ready_label_present,
                required_worker_capability=str(case.required_worker_capability or "").strip(),
                required_worker_label=str(case.required_worker_label or "").strip(),
                required_worker_label_present=bool(case.required_worker_label_present),
                decision_gate=DecisionGateResult(
                    triggered=False,
                    reason="Decision Gate not required",
                    missing_sections=(),
                    questions=(),
                    recommendation="Proceed with execution.",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
            )
        if str(getattr(normalized_pre_check, "outcome", "") or "").strip() == "decision_gate_required":
            normalized_pre_check = replace(normalized_pre_check, outcome="ready_for_agent")
        return IngressDecision(
            source=source,  # type: ignore[arg-type]
            pre_check=normalized_pre_check,
            block_reason=blocking_reason_for_precheck(normalized_pre_check),
            guidance=enqueue_reason_guidance(blocking_reason_for_precheck(normalized_pre_check))
            if blocking_reason_for_precheck(normalized_pre_check)
            else None,
            policy_error=None,
            label_actions=(),
        )
    return IngressDecision(
        source=source,  # type: ignore[arg-type]
        pre_check=pre_check,
        block_reason=str(snapshot.get("block_reason") or case.blocked_reason or "").strip() or None,
        guidance=str(snapshot.get("guidance") or "").strip() or None,
        policy_error=str(snapshot.get("policy_error") or "").strip() or None,
        label_actions=(),
    )


def normalize_clear_case_snapshot(
    *,
    case: DecisionCase,
    source: str,
    occurred_at: datetime | None = None,
) -> bool:
    classification = str(case.classification or "").strip() or "clear"
    if classification != "clear":
        return False
    metadata = dict(case.metadata_json) if isinstance(case.metadata_json, dict) else {}
    snapshot = metadata.get("result_snapshot") if isinstance(metadata.get("result_snapshot"), dict) else {}
    decision = decision_from_snapshot(
        snapshot=snapshot,
        source=source,
        classification="clear",
        cycle=None,
        case=case,
    )
    issue_labels = [
        str(label).strip()
        for label in snapshot.get("issue_labels", [])
        if str(label).strip()
    ]
    auto_resolved_slots = list(string_tuple(snapshot.get("auto_resolved_slots")))
    normalized_snapshot = serialize_result_snapshot(
        decision=decision,
        classification="clear",
        issue_labels=issue_labels,
        missing_slots=[],
        auto_resolved_slots=auto_resolved_slots,
    )
    changed = (
        str(case.blocked_reason or "").strip() != ""
        or snapshot != normalized_snapshot
        or str(case.state or "").strip() == "blocked_decision_gate"
        or str(case.state or "").strip() == "blocked_gtd"
    )
    if not changed:
        return False
    metadata["result_snapshot"] = normalized_snapshot
    case.blocked_reason = None
    case.classification = "clear"
    case.metadata_json = metadata
    case.updated_at = occurred_at or datetime.now(timezone.utc)
    return True


def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def worker_blocking_gate(
    *,
    pre_check: object | None,
    classification: str,
    block_reason: str | None,
) -> DecisionGateResult | None:
    if not isinstance(pre_check, PreRunCheckResult):
        return None
    if classification in {"decision_gate", "both"} and pre_check.decision_gate.triggered:
        return pre_check.decision_gate
    if classification not in {"gtd", "both"}:
        return None
    questions = tuple(
        str(question).strip()
        for question in pre_check.gtd.clarification_questions
        if str(question).strip()
    )
    missing = tuple(
        str(item).strip()
        for item in pre_check.gtd.missing_criteria
        if str(item).strip()
    )
    reason = decision_reason(pre_check=pre_check, classification=classification) or (
        "Good To Do details are incomplete" if block_reason == "gtd_required" else "Clarification required"
    )
    return DecisionGateResult(
        triggered=True,
        reason=reason,
        missing_sections=missing,
        questions=questions,
        recommendation="Clarification required before execution.",
        tags=(),
    )


def serialize_result_snapshot(
    *,
    decision: IngressDecision,
    classification: str,
    issue_labels: list[str],
    missing_slots: list[str],
    auto_resolved_slots: list[str],
) -> dict[str, object]:
    return {
        "classification": classification,
        "issue_labels": [str(label).strip() for label in issue_labels if str(label).strip()],
        "missing_slots": [str(slot).strip() for slot in missing_slots if str(slot).strip()],
        "auto_resolved_slots": [str(slot).strip() for slot in auto_resolved_slots if str(slot).strip()],
        "block_reason": decision.block_reason,
        "guidance": decision.guidance,
        "policy_error": decision.policy_error,
        "pre_check": serialize_precheck_result(decision.pre_check),
    }


def serialize_precheck_result(pre_check: object | None) -> dict[str, object] | None:
    if not isinstance(pre_check, PreRunCheckResult):
        return None
    return {
        "outcome": pre_check.outcome,
        "ready_label": pre_check.ready_label,
        "ready_label_present": pre_check.ready_label_present,
        "required_worker_capability": pre_check.required_worker_capability,
        "required_worker_label": pre_check.required_worker_label,
        "required_worker_label_present": pre_check.required_worker_label_present,
        "decision_gate": {
            "triggered": pre_check.decision_gate.triggered,
            "reason": pre_check.decision_gate.reason,
            "missing_sections": list(pre_check.decision_gate.missing_sections),
            "questions": list(pre_check.decision_gate.questions),
            "recommendation": pre_check.decision_gate.recommendation,
            "tags": list(pre_check.decision_gate.tags),
        },
        "gtd": {
            "valid": pre_check.gtd.valid,
            "missing_criteria": list(pre_check.gtd.missing_criteria),
            "clarification_questions": list(pre_check.gtd.clarification_questions),
        },
    }


def deserialize_precheck_result(value: object) -> PreRunCheckResult | None:
    if not isinstance(value, dict):
        return None
    decision_gate_payload = value.get("decision_gate")
    gtd_payload = value.get("gtd")
    if not isinstance(decision_gate_payload, dict) or not isinstance(gtd_payload, dict):
        return None
    return PreRunCheckResult(
        outcome=str(value.get("outcome") or "").strip(),
        ready_label=str(value.get("ready_label") or "").strip() or None,
        ready_label_present=bool(value.get("ready_label_present", False)),
        required_worker_capability=str(value.get("required_worker_capability") or "").strip(),
        required_worker_label=str(value.get("required_worker_label") or "").strip(),
        required_worker_label_present=bool(value.get("required_worker_label_present", False)),
        decision_gate=DecisionGateResult(
            triggered=bool(decision_gate_payload.get("triggered")),
            reason=str(decision_gate_payload.get("reason") or "").strip(),
            missing_sections=string_tuple(decision_gate_payload.get("missing_sections")),
            questions=string_tuple(decision_gate_payload.get("questions")),
            recommendation=str(decision_gate_payload.get("recommendation") or "").strip(),
            tags=string_tuple(decision_gate_payload.get("tags")),
        ),
        gtd=GoodToDoValidationResult(
            valid=bool(gtd_payload.get("valid")),
            missing_criteria=string_tuple(gtd_payload.get("missing_criteria")),
            clarification_questions=string_tuple(gtd_payload.get("clarification_questions")),
        ),
    )


def apply_frozen_cycle_to_precheck(*, pre_check: object, cycle: DecisionCycle, classification: str) -> object:
    unresolved_ids = {
        str(question_id).strip()
        for question_id in cycle.unresolved_question_ids_json
        if str(question_id).strip()
    }
    decision_gate_questions = [
        str(item.get("text") or "").strip()
        for item in cycle.question_set_json
        if (
            str(item.get("kind") or "").strip() == QUESTION_KIND_DG
            and str(item.get("text") or "").strip()
            and (
                not str(item.get("id") or "").strip()
                or str(item.get("id") or "").strip() in unresolved_ids
            )
        )
    ]
    gtd_questions = [
        str(item.get("text") or "").strip()
        for item in cycle.question_set_json
        if (
            str(item.get("kind") or "").strip() == QUESTION_KIND_GTD
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
    if classification in {"decision_gate", "both"} and decision_gate is not None:
        next_decision_gate = decision_gate
        if cycle.reason:
            next_decision_gate = replace(next_decision_gate, reason=cycle.reason)
        next_decision_gate = replace(next_decision_gate, questions=tuple(decision_gate_questions))
        resolved = replace(resolved, decision_gate=next_decision_gate)
    if classification in {"gtd", "both"} and gtd is not None:
        resolved = replace(resolved, gtd=replace(gtd, clarification_questions=tuple(gtd_questions)))
    return resolved


def build_question_set(*, pre_check: object, classification: str) -> list[dict]:
    if pre_check is None:
        return []
    items: list[dict] = []
    if classification in {"decision_gate", "both"}:
        decision_gate = getattr(pre_check, "decision_gate", None)
        questions = getattr(decision_gate, "questions", ()) if decision_gate is not None else ()
        for question in questions:
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"dg_{stable_short_hash(text)}",
                    "kind": QUESTION_KIND_DG,
                    "text": text,
                }
            )
    if classification in {"gtd", "both"}:
        for question in getattr(pre_check, "gtd_clarification_questions", ()):
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"gtd_{stable_short_hash(text)}",
                    "kind": QUESTION_KIND_GTD,
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


def stable_short_hash(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:10]


def case_state_for_decision(*, decision: IngressDecision) -> str:
    pre_check = decision.pre_check
    if decision.block_reason == "decision_gate_required":
        return "blocked_decision_gate"
    if decision.block_reason == "gtd_required":
        return "blocked_gtd"
    if pre_check is not None and str(getattr(pre_check, "outcome", "") or "").strip() == "ready_for_agent":
        return "ready_for_execution"
    return "clear"


def decision_reason(*, pre_check: object, classification: str) -> str | None:
    if pre_check is None:
        return None
    if classification in {"decision_gate", "both"}:
        decision_gate = getattr(pre_check, "decision_gate", None)
        reason = str(getattr(decision_gate, "reason", "") or "").strip() if decision_gate is not None else ""
        if reason:
            return reason
    if classification in {"gtd", "both"}:
        missing = [
            str(item).strip()
            for item in getattr(pre_check, "gtd_missing_criteria", ())
            if str(item).strip()
        ]
        if missing:
            return "Missing GTD criteria: " + ", ".join(missing)
    return None


def issue_fingerprint(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str],
) -> str:
    payload = json.dumps(
        {
            "summary": str(issue_summary or "").strip(),
            "description": str(issue_description or "").strip(),
            "labels": sorted({label.casefold() for label in issue_labels}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_idempotency_key(*, tenant_id: str, project_id: str | None, event: DecisionEventInput) -> str:
    if isinstance(event.idempotency_key, str) and event.idempotency_key.strip():
        return event.idempotency_key.strip()
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "source": event.source,
            "event_type": event.event_type,
            "issue_key": event.issue_key,
            "issue_summary": str(event.issue_summary or "").strip(),
            "issue_description": str(event.issue_description or "").strip(),
            "labels": sorted(str(label).strip().casefold() for label in (event.issue_labels or []) if str(label).strip()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_occurred_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def merge_case_metadata(
    *,
    existing_metadata: object,
    auto_resolved_answers: dict[str, SlotResolution],
    classification: str,
    issue_labels: list[str],
    decision: IngressDecision,
    serialize_slot_resolution_fn,
) -> dict[str, object]:
    metadata = dict(existing_metadata) if isinstance(existing_metadata, dict) else {}
    stored_answers = metadata.get("auto_resolved_answers")
    resolved_answers = dict(stored_answers) if isinstance(stored_answers, dict) else {}
    for slot_name, answer in auto_resolved_answers.items():
        resolved_answers[slot_name] = serialize_slot_resolution_fn(answer)
    metadata["auto_resolved_answers"] = resolved_answers
    metadata["auto_resolved_slots"] = sorted(resolved_answers.keys())
    metadata["result_snapshot"] = serialize_result_snapshot(
        decision=decision,
        classification=classification,
        issue_labels=issue_labels,
        missing_slots=precheck_missing_slots(decision.pre_check) if decision.pre_check is not None else [],
        auto_resolved_slots=sorted(resolved_answers.keys()),
    )
    return metadata
