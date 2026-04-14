from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID,
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    PMInterviewBrief,
    _normalized_text,
    normalize_pm_interview_brief,
    upsert_pm_interview_case,
)
from orchestrator.storage.models import PMInterviewCase


def resolve_parent_feature_brief(
    *,
    session: Session,
    tenant_id: str,
    parent_issue_key: str,
    include_incomplete: bool = False,
) -> PMInterviewBrief | None:
    normalized_tenant_id = _normalized_text(tenant_id)
    normalized_parent_issue_key = _normalized_text(parent_issue_key).upper()
    if not normalized_tenant_id or not normalized_parent_issue_key:
        return None
    stmt = select(PMInterviewCase).where(
        PMInterviewCase.tenant_id == normalized_tenant_id,
        PMInterviewCase.parent_issue_key == normalized_parent_issue_key,
    )
    if not include_incomplete:
        stmt = stmt.where(PMInterviewCase.status == PM_INTERVIEW_STATUS_PM_COMPLETED)
    row = session.execute(
        stmt.order_by(PMInterviewCase.updated_at.desc(), PMInterviewCase.created_at.desc())
    ).scalars().first()
    if row is None:
        return None
    return normalize_pm_interview_brief(getattr(row, "brief_json", None) or None)


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
        select(PMInterviewCase)
        .where(
            PMInterviewCase.tenant_id == normalized_tenant_id,
            PMInterviewCase.parent_issue_key == normalized_parent_issue_key,
            PMInterviewCase.source_kind != PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
        )
        .order_by(PMInterviewCase.updated_at.desc(), PMInterviewCase.created_at.desc())
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
        brief=brief,
        notes={
            "parent_brief_snapshot": True,
            **dict(notes or {}),
        },
    )
