from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.sql import or_

from orchestrator.storage.models import Run


def build_runs_query(
    *,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    issue_query: str | None,
    pr_state: str | None,
    from_time: datetime | None,
    to_time: datetime | None,
    limit: int,
    offset: int,
):
    query = select(Run).order_by(Run.created_at.desc())

    if tenant_id:
        query = query.where(Run.tenant_id == tenant_id)
    if project_id:
        query = query.where(Run.project_id == project_id)
    if status_filter:
        query = query.where(Run.status == status_filter)
    normalized_issue_query = (issue_query or "").strip()
    if normalized_issue_query:
        issue_pattern = f"%{normalized_issue_query}%"
        query = query.where(
            or_(
                Run.issue_key.ilike(issue_pattern),
                Run.issue_summary.ilike(issue_pattern),
            )
        )
    normalized_pr_state = (pr_state or "").strip().lower()
    if normalized_pr_state == "none":
        query = query.where(or_(Run.pr_url.is_(None), Run.pr_url == ""))
    elif normalized_pr_state == "has_value":
        query = query.where(Run.pr_url.is_not(None)).where(Run.pr_url != "")
    if from_time:
        query = query.where(Run.created_at >= from_time)
    if to_time:
        query = query.where(Run.created_at <= to_time)
    safe_limit = max(1, min(limit, 200))
    safe_offset = max(0, offset)
    return query.limit(safe_limit).offset(safe_offset)
