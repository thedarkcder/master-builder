from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.decision.planner import DecisionPlannerQuestion, DecisionPlannerResult
from orchestrator.core.runtime.payload_models import PlannerGateStatus
from orchestrator.storage.models import TenantMembership, TenantUser

DECISION_OWNER_QUESTION_ID = "decision_owner"
_IMPLICIT_OWNER_DETAIL = "Single-member account; the accountable decision owner is implicit."


def tenant_has_multiple_active_members(*, session: Session, tenant_id: str) -> bool:
    rows = session.execute(
        select(TenantMembership.user_id)
        .join(TenantUser, TenantUser.user_id == TenantMembership.user_id)
        .where(
            TenantMembership.tenant_id == tenant_id,
            TenantUser.is_active.is_(True),
        )
        .distinct()
        .limit(2)
    ).scalars()
    return len(tuple(rows)) > 1


def enforce_decision_owner_account_policy(
    *,
    planner_result: DecisionPlannerResult,
    decision_owner_required: bool,
) -> DecisionPlannerResult:
    if decision_owner_required:
        return planner_result

    removed_owner_question = any(_is_decision_owner_question(item) for item in planner_result.questions)
    owner_state_present = any(_is_decision_owner_question(item) for item in planner_result.question_states)
    if not removed_owner_question and not owner_state_present and DECISION_OWNER_QUESTION_ID not in planner_result.missing_items:
        return planner_result

    questions = tuple(item for item in planner_result.questions if not _is_decision_owner_question(item))
    question_states = tuple(_resolve_owner_question_state(item) for item in planner_result.question_states)
    if removed_owner_question and not owner_state_present:
        question_states = question_states + (
            DecisionPlannerQuestion(
                question_id=DECISION_OWNER_QUESTION_ID,
                kind="decision_gate",
                question="Who is the accountable decision owner?",
                status="accepted",
                detail=_IMPLICIT_OWNER_DETAIL,
            ),
        )

    return DecisionPlannerResult(
        gate_status=_gate_status_for_questions(questions),
        reason=planner_result.reason if questions else "",
        questions=questions,
        question_states=question_states,
        resolved_items=_append_unique(planner_result.resolved_items, DECISION_OWNER_QUESTION_ID),
        missing_items=tuple(item for item in planner_result.missing_items if item != DECISION_OWNER_QUESTION_ID),
        captured_answer_summary=_append_owner_summary(planner_result.captured_answer_summary),
    )


def _is_decision_owner_question(item: DecisionPlannerQuestion) -> bool:
    return item.question_id == DECISION_OWNER_QUESTION_ID


def _resolve_owner_question_state(item: DecisionPlannerQuestion) -> DecisionPlannerQuestion:
    if not _is_decision_owner_question(item):
        return item
    return DecisionPlannerQuestion(
        question_id=item.question_id,
        kind=item.kind,
        question=item.question,
        status="accepted",
        detail=_IMPLICIT_OWNER_DETAIL,
    )


def _gate_status_for_questions(questions: tuple[DecisionPlannerQuestion, ...]) -> PlannerGateStatus:
    has_decision_gate = any(item.kind == "decision_gate" for item in questions)
    has_gtd = any(item.kind == "gtd" for item in questions)
    if has_decision_gate and has_gtd:
        return PlannerGateStatus.BLOCKED_BOTH
    if has_decision_gate:
        return PlannerGateStatus.BLOCKED_DECISION_GATE
    if has_gtd:
        return PlannerGateStatus.BLOCKED_GTD
    return PlannerGateStatus.CLEAR


def _append_unique(items: tuple[str, ...], item: str) -> tuple[str, ...]:
    if item in items:
        return items
    return items + (item,)


def _append_owner_summary(summary: str | None) -> str:
    owner_summary = "Decision owner not requested because the account has a single active member."
    if not summary:
        return owner_summary
    if owner_summary in summary:
        return summary
    return f"{summary}\n{owner_summary}"
