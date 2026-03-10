from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import select

from orchestrator.core.decision_effect_service import enqueue_cycle_comment_effect
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
from orchestrator.storage.models import DecisionCase, DecisionCycle, DecisionEvent


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
    accepted_question_ids: set[str],
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
                    if question_id in current_question_ids and question_id not in accepted_question_ids
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
                    if str(item.get("id") or "").strip() and str(item.get("id") or "").strip() not in accepted_question_ids
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
        outbox_effect_ids.append(
            enqueue_cycle_comment_effect(
                session=session,
                case=case,
                cycle=cycle,
                now=occurred_at,
            )
        )

    session.commit()
    session.refresh(case)
    if cycle is not None:
        session.refresh(cycle)
    return case, cycle, tuple(outbox_effect_ids)
