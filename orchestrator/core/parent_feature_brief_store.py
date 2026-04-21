from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.clarification_projection_service import resolve_active_clarification_context
from orchestrator.core.followup_context_service import FOLLOWUP_CONTEXT_PM_INTERVIEW
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_STATUS_DRAFTING,
    PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID,
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    PM_INTERVIEW_STATUS_READY_TO_WRITE,
    PM_INTERVIEW_STATUS_RESEARCHING,
    PMInterviewBrief,
    _normalized_text,
    normalize_pm_interview_brief,
    upsert_pm_interview_case,
)
from orchestrator.storage.models import PMInterviewCase


_PM_INTERVIEW_CLARIFICATION_STATUSES = frozenset(
    {
        PM_INTERVIEW_STATUS_DRAFTING,
        PM_INTERVIEW_STATUS_QUESTION_PENDING,
        PM_INTERVIEW_STATUS_RESEARCHING,
    }
)
_PM_INTERVIEW_DRAFT_STATUSES = frozenset(
    {
        *_PM_INTERVIEW_CLARIFICATION_STATUSES,
        PM_INTERVIEW_STATUS_READY_TO_WRITE,
        PM_INTERVIEW_STATUS_PM_COMPLETED,
    }
)


@dataclass(frozen=True)
class ParentFeatureBriefReadiness:
    canonical_brief: PMInterviewBrief | None
    draft_brief: PMInterviewBrief | None
    clarification_open: bool
    clarification_questions: tuple[str, ...]
    pm_interview_status: str | None = None
    has_active_followup: bool = False

    @property
    def ready_for_planning(self) -> bool:
        return self.canonical_brief is not None and not self.clarification_open


def _brief_without_open_questions(brief: PMInterviewBrief | None) -> PMInterviewBrief | None:
    if brief is None:
        return None
    return replace(brief, open_questions=())


def _primary_parent_feature_case_stmt(*, tenant_id: str, parent_issue_key: str):
    return (
        select(PMInterviewCase)
        .where(
            PMInterviewCase.tenant_id == tenant_id,
            PMInterviewCase.parent_issue_key == parent_issue_key,
            PMInterviewCase.source_kind != PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
        )
        .order_by(PMInterviewCase.updated_at.desc(), PMInterviewCase.created_at.desc())
    )


def _parent_feature_snapshot_stmt(*, tenant_id: str, parent_issue_key: str):
    return (
        select(PMInterviewCase)
        .where(
            PMInterviewCase.tenant_id == tenant_id,
            PMInterviewCase.parent_issue_key == parent_issue_key,
            PMInterviewCase.source_kind == PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
        )
        .order_by(PMInterviewCase.updated_at.desc(), PMInterviewCase.created_at.desc())
    )


def _question_from_payload(value: object) -> str | None:
    if isinstance(value, Mapping):
        question = str(value.get("question") or "").strip()
        if question:
            return question
    question = str(value or "").strip()
    return question or None


def _clarification_questions_from_case(row: PMInterviewCase | None) -> tuple[str, ...]:
    if row is None:
        return ()
    questions: list[str] = []
    for candidate in (
        getattr(row, "current_question_json", None),
        getattr(row, "next_question_json", None),
    ):
        question = _question_from_payload(candidate)
        if question and question not in questions:
            questions.append(question)
    return tuple(questions)


def _clarification_questions_from_followup(*, followup_context) -> tuple[str, ...]:
    if followup_context is None:
        return ()
    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    questions: list[str] = []
    for candidate in metadata.get("questions", []) if isinstance(metadata.get("questions"), list) else []:
        question = _question_from_payload(candidate)
        if question and question not in questions:
            questions.append(question)
    return tuple(questions)


def resolve_parent_feature_brief_readiness(
    *,
    session: Session,
    tenant_id: str,
    parent_issue_key: str,
) -> ParentFeatureBriefReadiness:
    normalized_tenant_id = _normalized_text(tenant_id)
    normalized_parent_issue_key = _normalized_text(parent_issue_key).upper()
    if not normalized_tenant_id or not normalized_parent_issue_key:
        return ParentFeatureBriefReadiness(
            canonical_brief=None,
            draft_brief=None,
            clarification_open=False,
            clarification_questions=(),
        )

    snapshot_row = session.execute(
        _parent_feature_snapshot_stmt(
            tenant_id=normalized_tenant_id,
            parent_issue_key=normalized_parent_issue_key,
        ).where(PMInterviewCase.status == PM_INTERVIEW_STATUS_PM_COMPLETED)
    ).scalars().first()
    primary_case = session.execute(
        _primary_parent_feature_case_stmt(
            tenant_id=normalized_tenant_id,
            parent_issue_key=normalized_parent_issue_key,
        )
    ).scalars().first()
    pm_interview_status = str(getattr(primary_case, "status", "") or "").strip().lower() or None
    active_followup = resolve_active_clarification_context(
        session=session,
        tenant_id=normalized_tenant_id,
        issue_key=normalized_parent_issue_key,
        context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
    )

    canonical_brief = _brief_without_open_questions(
        normalize_pm_interview_brief(getattr(snapshot_row, "brief_json", None) or None)
        if snapshot_row is not None
        else None
    )
    draft_brief = _brief_without_open_questions(
        normalize_pm_interview_brief(getattr(primary_case, "brief_json", None) or None)
        if primary_case is not None and pm_interview_status in _PM_INTERVIEW_DRAFT_STATUSES
        else None
    )

    clarification_open = False
    clarification_questions: tuple[str, ...] = ()
    if canonical_brief is None:
        clarification_open = active_followup is not None or pm_interview_status in _PM_INTERVIEW_CLARIFICATION_STATUSES
        if clarification_open:
            clarification_questions = _clarification_questions_from_followup(followup_context=active_followup)
            if not clarification_questions:
                clarification_questions = _clarification_questions_from_case(primary_case)

    return ParentFeatureBriefReadiness(
        canonical_brief=canonical_brief,
        draft_brief=draft_brief,
        clarification_open=clarification_open,
        clarification_questions=clarification_questions,
        pm_interview_status=pm_interview_status,
        has_active_followup=active_followup is not None,
    )


def resolve_parent_feature_brief(
    *,
    session: Session,
    tenant_id: str,
    parent_issue_key: str,
    include_incomplete: bool = False,
) -> PMInterviewBrief | None:
    readiness = resolve_parent_feature_brief_readiness(
        session=session,
        tenant_id=tenant_id,
        parent_issue_key=parent_issue_key,
    )
    if readiness.canonical_brief is not None:
        return readiness.canonical_brief
    if include_incomplete:
        return readiness.draft_brief
    return None


def resolve_parent_feature_case(
    *,
    session: Session,
    tenant_id: str,
    parent_issue_key: str,
) -> PMInterviewCase | None:
    normalized_tenant_id = _normalized_text(tenant_id)
    normalized_parent_issue_key = _normalized_text(parent_issue_key).upper()
    if not normalized_tenant_id or not normalized_parent_issue_key:
        return None
    primary_row = session.execute(
        _primary_parent_feature_case_stmt(
            tenant_id=normalized_tenant_id,
            parent_issue_key=normalized_parent_issue_key,
        )
    ).scalars().first()
    if primary_row is not None:
        return primary_row
    return session.execute(
        select(PMInterviewCase)
        .where(
            PMInterviewCase.tenant_id == normalized_tenant_id,
            PMInterviewCase.parent_issue_key == normalized_parent_issue_key,
        )
        .order_by(PMInterviewCase.updated_at.desc(), PMInterviewCase.created_at.desc())
    ).scalars().first()


def persist_parent_feature_brief_snapshot(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    parent_issue_key: str,
    source_text: str,
    brief: Mapping[str, Any] | PMInterviewBrief,
    notes: Mapping[str, Any] | None = None,
    status: str = PM_INTERVIEW_STATUS_PM_COMPLETED,
) -> PMInterviewCase:
    normalized_parent_issue_key = _normalized_text(parent_issue_key).upper()
    if not normalized_parent_issue_key:
        raise ValueError("parent_issue_key is required")
    sanitized_brief = _brief_without_open_questions(normalize_pm_interview_brief(brief))
    return upsert_pm_interview_case(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        request_id=f"parent-brief:{normalized_parent_issue_key}",
        source_kind=PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
        channel_id=PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID,
        source_text=source_text,
        status=status,
        parent_issue_key=normalized_parent_issue_key,
        brief=sanitized_brief,
        notes={
            "parent_brief_snapshot": True,
            **dict(notes or {}),
        },
    )
