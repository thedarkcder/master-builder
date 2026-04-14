from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.decision_snapshot_codec import (
    DecisionResultSnapshot,
    PrecheckSnapshot,
    apply_frozen_cycle_questions,
    build_question_set as build_question_set_codec,
)
from orchestrator.core.knowledge_base import SlotResolution
from orchestrator.core.pre_run_check import PreRunCheckResult, evaluate_pre_run_check
from orchestrator.core.precheck_decision import precheck_missing_slots
from orchestrator.core.decision_state_machine import decision_from_snapshot
from orchestrator.core.decision_state_machine import resolve_execution_gate_state
from orchestrator.core.decision_types import (
    DecisionClassification,
    DecisionSource,
    DecisionEventInput,
    DecisionLabelAction,
    IngressDecision,
    PrecheckOutcome,
    tenant_ready_label,
)
from orchestrator.storage.models import DecisionCycle

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
        ready_label=tenant_ready_label(tenant),
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
    outcome = PrecheckOutcome.parse(getattr(pre_check, "outcome", None))
    if ready_label_missing and ready_label and outcome is PrecheckOutcome.MISSING_READY_LABEL:
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
    classification = DecisionClassification.parse(snapshot.get("classification"))
    missing_slots = string_tuple(snapshot.get("missing_slots"))
    auto_resolved_slots = string_tuple(snapshot.get("auto_resolved_slots"))

    case = session.get(decision_case_type, existing_event.case_id)
    if case is None:
        raise RuntimeError(f"Decision event {existing_event.event_id} references missing case {existing_event.case_id}")
    cycle = session.get(decision_cycle_type, existing_event.cycle_id) if existing_event.cycle_id else None
    decision = decision_from_snapshot(
        snapshot=snapshot,
        source=str(existing_event.source or "jira_webhook"),
        classification=classification.value,
        cycle=cycle,
        case=case,
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
    execution_gate = resolve_execution_gate_state(decision=decision, classification=classification)
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


def string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def serialize_result_snapshot(
    *,
    decision: IngressDecision,
    classification: str,
    issue_labels: list[str],
    missing_slots: list[str],
    auto_resolved_slots: list[str],
) -> dict[str, object]:
    return DecisionResultSnapshot.from_decision(
        decision=decision,
        classification=classification,
        issue_labels=issue_labels,
        missing_slots=missing_slots,
        auto_resolved_slots=auto_resolved_slots,
    ).dump()


def serialize_precheck_result(pre_check: object | None) -> dict[str, object] | None:
    if not isinstance(pre_check, PreRunCheckResult):
        return None
    return PrecheckSnapshot.from_precheck(pre_check).dump()


def deserialize_precheck_result(value: object) -> PreRunCheckResult | None:
    snapshot = PrecheckSnapshot.load(value)
    return snapshot.to_precheck() if snapshot is not None else None


def apply_frozen_cycle_to_precheck(*, pre_check: object, cycle: DecisionCycle, classification: str) -> object:
    return apply_frozen_cycle_questions(
        pre_check=pre_check,
        cycle_question_set=list(cycle.question_set_json),
        unresolved_question_ids=list(cycle.unresolved_question_ids_json),
        cycle_reason=cycle.reason,
        classification=DecisionClassification.parse(classification),
    )


def build_question_set(*, pre_check: object, classification: str) -> list[dict]:
    return build_question_set_codec(pre_check=pre_check, classification=classification)


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
