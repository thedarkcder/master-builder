from __future__ import annotations

from datetime import datetime
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from orchestrator.core.decision_precheck_mapping import (
    BLOCKED_CLASSIFICATIONS,
    build_question_set,
    case_state_for_decision,
    decision_reason,
    merge_case_metadata,
    serialize_result_snapshot,
)
from orchestrator.core.decision_resolution_service import serialize_slot_resolution
from orchestrator.core.precheck_decision import precheck_missing_slots
from orchestrator.storage.models import DecisionCase, DecisionCycle, DecisionEffectOutbox, DecisionEvent


def active_cycle(*, session, case: DecisionCase) -> DecisionCycle | None:
    if not case.active_cycle_id:
        return None
    cycle = session.get(DecisionCycle, case.active_cycle_id)
    if cycle is None:
        return None
    if cycle.status != "open":
        return None
    return cycle


def load_or_create_case(
    *,
    session,
    tenant,
    project,
    issue_key: str,
    now: datetime,
) -> DecisionCase:
    case = session.execute(
        select(DecisionCase).where(
            DecisionCase.tenant_id == tenant.tenant_id,
            DecisionCase.issue_key == issue_key,
        )
    ).scalar_one_or_none()
    if case is not None:
        return case
    case = DecisionCase(
        case_id=uuid4().hex,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else None,
        issue_key=issue_key,
        state="clear",
        blocked_reason=None,
        classification=None,
        issue_fingerprint=None,
        active_cycle_id=None,
        last_source=None,
        last_event_type=None,
        last_event_at=None,
        required_worker_capability=None,
        required_worker_label=None,
        ready_label=None,
        ready_label_present=False,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    session.add(case)
    session.flush()
    return case


def existing_case_for_issue(*, session, tenant_id: str, issue_key: str) -> DecisionCase | None:
    return session.execute(
        select(DecisionCase).where(
            DecisionCase.tenant_id == tenant_id,
            DecisionCase.issue_key == issue_key,
        )
    ).scalar_one_or_none()


def build_cycle_comment(*, case: DecisionCase, cycle: DecisionCycle) -> str:
    unresolved_ids = {
        str(question_id).strip()
        for question_id in cycle.unresolved_question_ids_json
        if str(question_id).strip()
    }
    lines = [
        f"<!-- decision-cycle:{cycle.cycle_id} -->",
        f"Decision state: `{case.state}`",
    ]
    if cycle.reason:
        lines.append(f"Reason: {cycle.reason}")
    if cycle.question_set_json:
        lines.append("Outstanding questions:")
        for item in cycle.question_set_json:
            question_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if unresolved_ids and question_id and question_id not in unresolved_ids:
                continue
            if question_id:
                lines.append(f"- [{question_id}] {text}")
            else:
                lines.append(f"- {text}")
    return "\n".join(lines)


def enqueue_or_get_effect(
    *,
    session,
    case: DecisionCase,
    cycle: DecisionCycle,
    effect_type: str,
    dedupe_key: str,
    payload: dict[str, object],
    now: datetime,
) -> DecisionEffectOutbox:
    existing = session.execute(
        select(DecisionEffectOutbox).where(
            DecisionEffectOutbox.tenant_id == case.tenant_id,
            DecisionEffectOutbox.dedupe_key == dedupe_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    effect = DecisionEffectOutbox(
        effect_id=uuid4().hex,
        dedupe_key=dedupe_key,
        case_id=case.case_id,
        cycle_id=cycle.cycle_id,
        tenant_id=case.tenant_id,
        project_id=case.project_id,
        issue_key=case.issue_key,
        effect_type=effect_type,
        payload_json=payload,
        status="pending",
        attempt_count=0,
        next_attempt_at=None,
        sent_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(effect)
    return effect


def persist_decision_state(
    *,
    session,
    tenant,
    project,
    event,
    occurred_at: datetime,
    idempotency_key: str,
    issue_labels: list[str],
    issue_description: str | None,
    decision,
    classification: str,
    auto_resolved_answers,
    issue_fingerprint_fn,
) -> tuple[DecisionCase, DecisionCycle | None, tuple[str, ...]]:
    case = load_or_create_case(
        session=session,
        tenant=tenant,
        project=project,
        issue_key=event.issue_key,
        now=occurred_at,
    )
    pre_check = decision.pre_check
    case_state = case_state_for_decision(decision=decision)
    question_driven = classification in BLOCKED_CLASSIFICATIONS and decision.block_reason in {
        "decision_gate_required",
        "gtd_required",
    }
    question_set = build_question_set(pre_check=pre_check, classification=classification) if question_driven else []
    reason = decision_reason(pre_check=pre_check, classification=classification) if question_driven else None

    current_active_cycle = active_cycle(session=session, case=case)
    cycle: DecisionCycle | None = None
    if question_driven:
        if (
            current_active_cycle is not None
            and current_active_cycle.status == "open"
            and str(current_active_cycle.classification or "") == classification
        ):
            cycle = current_active_cycle
            current_question_ids = {
                str(item.get("id") or "").strip()
                for item in question_set
                if str(item.get("id") or "").strip()
            }
            if current_question_ids:
                cycle.unresolved_question_ids_json = [
                    question_id
                    for question_id in cycle.unresolved_question_ids_json
                    if question_id in current_question_ids
                ]
            cycle.reason = cycle.reason or reason
            cycle.updated_at = occurred_at
        else:
            if current_active_cycle is not None and current_active_cycle.status == "open":
                current_active_cycle.status = "closed"
                current_active_cycle.closed_at = occurred_at
                current_active_cycle.updated_at = occurred_at
            cycle = DecisionCycle(
                cycle_id=uuid4().hex,
                case_id=case.case_id,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id if project is not None else None,
                issue_key=event.issue_key,
                status="open",
                reason=reason,
                classification=classification,
                question_set_json=question_set,
                unresolved_question_ids_json=[
                    str(item.get("id") or "")
                    for item in question_set
                    if str(item.get("id") or "").strip()
                ],
                metadata_json={},
                opened_at=occurred_at,
                closed_at=None,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            session.add(cycle)
            case.active_cycle_id = cycle.cycle_id
    else:
        if current_active_cycle is not None and current_active_cycle.status == "open":
            current_active_cycle.status = "closed"
            current_active_cycle.closed_at = occurred_at
            current_active_cycle.updated_at = occurred_at
        case.active_cycle_id = None

    case.project_id = project.project_id if project is not None else case.project_id
    case.state = case_state
    case.blocked_reason = decision.block_reason
    case.classification = classification
    case.issue_fingerprint = issue_fingerprint_fn(
        issue_summary=event.issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
    )
    case.last_source = event.source
    case.last_event_type = str(event.event_type or "").strip() or None
    case.last_event_at = occurred_at
    case.required_worker_capability = (
        str(getattr(pre_check, "required_worker_capability", "") or "").strip() or None
        if pre_check is not None
        else None
    )
    case.required_worker_label = (
        str(getattr(pre_check, "required_worker_label", "") or "").strip() or None
        if pre_check is not None
        else None
    )
    case.ready_label = (
        str(getattr(pre_check, "ready_label", "") or "").strip() or None
        if pre_check is not None
        else None
    )
    case.ready_label_present = bool(getattr(pre_check, "ready_label_present", False)) if pre_check is not None else False
    case.metadata_json = merge_case_metadata(
        existing_metadata=case.metadata_json,
        auto_resolved_answers=auto_resolved_answers,
        classification=classification,
        issue_labels=issue_labels,
        decision=decision,
        serialize_slot_resolution_fn=serialize_slot_resolution,
    )
    case.updated_at = occurred_at

    outbox_effect_ids: list[str] = []
    event_row = DecisionEvent(
        event_id=uuid4().hex,
        idempotency_key=idempotency_key,
        case_id=case.case_id,
        cycle_id=cycle.cycle_id if cycle is not None else None,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else None,
        issue_key=event.issue_key,
        source=event.source,
        event_type=str(event.event_type or "").strip() or "unspecified",
        payload_json={
            "issue_summary": str(event.issue_summary or "").strip(),
            "issue_labels": issue_labels,
            "result_snapshot": serialize_result_snapshot(
                decision=decision,
                classification=classification,
                issue_labels=issue_labels,
                missing_slots=precheck_missing_slots(pre_check) if pre_check is not None else [],
                auto_resolved_slots=sorted(auto_resolved_answers.keys()),
            ),
        },
        outcome_state=case_state,
        created_at=occurred_at,
    )
    session.add(event_row)

    if cycle is not None and cycle.status == "open":
        effect = enqueue_or_get_effect(
            session=session,
            case=case,
            cycle=cycle,
            effect_type="jira_comment",
            dedupe_key=f"jira-comment:{tenant.tenant_id}:{event.issue_key}:{cycle.cycle_id}",
            payload={
                "comment": build_cycle_comment(case=case, cycle=cycle),
            },
            now=occurred_at,
        )
        outbox_effect_ids.append(effect.effect_id)

    session.commit()
    session.refresh(case)
    if cycle is not None:
        session.refresh(cycle)
    return case, cycle, tuple(outbox_effect_ids)


def publish_decision_effects(
    *,
    session,
    effect_ids: tuple[str, ...],
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]],
    occurred_at: datetime,
) -> None:
    for effect_id in effect_ids:
        effect = session.get(DecisionEffectOutbox, effect_id)
        if effect is None or effect.status != "pending":
            continue
        if effect.effect_type != "jira_comment":
            continue
        posted, error = publish_jira_comment_fn(str(effect.payload_json.get("comment") or ""))
        if posted:
            effect.status = "sent"
            effect.sent_at = occurred_at
            effect.updated_at = occurred_at
        else:
            effect.status = "failed"
            effect.attempt_count += 1
            effect.last_error = error
            effect.updated_at = occurred_at
        session.commit()
