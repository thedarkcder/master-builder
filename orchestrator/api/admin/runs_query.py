from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from orchestrator.storage.models import Run


def build_runs_query(
    *,
    tenant_id: str | None,
    project_id: str | None,
    status_filter: str | None,
    from_time: datetime | None,
    to_time: datetime | None,
):
    query = select(Run).order_by(Run.created_at.desc())

    if tenant_id:
        query = query.where(Run.tenant_id == tenant_id)
    if project_id:
        query = query.where(Run.project_id == project_id)
    if status_filter:
        query = query.where(Run.status == status_filter)
    if from_time:
        query = query.where(Run.created_at >= from_time)
    if to_time:
        query = query.where(Run.created_at <= to_time)
    return query
