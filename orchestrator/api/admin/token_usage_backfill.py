from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from orchestrator.core.run_logs import NormalizedRunLogEvent, materialize_token_usage_from_log_message
from orchestrator.storage.models import Run, RunLogEvent

_TOKEN_STAGES = ("pm", "dev", "test", "review")
_MAX_BACKFILL_ROWS = 1000


def _normalize_issue_keys(issue_key: str | None) -> list[str]:
    if not issue_key:
        return []
    return [value.strip() for value in issue_key.split(",") if value.strip()]


def _to_normalized_row(row: RunLogEvent) -> NormalizedRunLogEvent:
    return NormalizedRunLogEvent(
        tenant_id=str(row.tenant_id),
        project_id=str(row.project_id) if row.project_id else None,
        run_id=str(row.run_id) if row.run_id else None,
        issue_key=str(row.issue_key) if row.issue_key else None,
        agent_id=str(row.agent_id),
        invocation_id=str(row.invocation_id or ""),
        channel=str(row.channel or ""),
        command=str(row.command or ""),
        working_dir=str(row.working_dir) if row.working_dir else None,
        stage=str(row.stage or "").strip().lower(),
        attempt=row.attempt,
        stream=str(row.stream),
        message=str(row.message),
        recorded_at=row.recorded_at,
    )


def materialize_token_usage_for_scope(
    *,
    session,
    tenant_id: str,
    project_id: str,
    issue_key: str | None = None,
    run_ids: list[str] | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    max_rows: int = _MAX_BACKFILL_ROWS,
) -> int:
    normalized_tenant = str(tenant_id).strip()
    normalized_project = str(project_id).strip()
    if not normalized_tenant or not normalized_project:
        return 0

    query = (
        select(RunLogEvent)
        .join(Run, Run.run_id == RunLogEvent.run_id)
        .where(Run.tenant_id == normalized_tenant)
        .where(Run.project_id == normalized_project)
        .where(RunLogEvent.run_id.is_not(None))
        .where(RunLogEvent.invocation_id.is_not(None))
        .where(RunLogEvent.channel.is_not(None))
        .where(RunLogEvent.command.is_not(None))
        .where(RunLogEvent.stage.in_(_TOKEN_STAGES))
        .order_by(RunLogEvent.recorded_at.desc(), RunLogEvent.event_id.desc())
        .limit(max(1, int(max_rows)))
    )
    issue_keys = _normalize_issue_keys(issue_key)
    if issue_keys:
        if len(issue_keys) == 1:
            query = query.where(Run.issue_key == issue_keys[0])
        else:
            query = query.where(Run.issue_key.in_(issue_keys))
    if run_ids:
        cleaned_run_ids = [str(value).strip() for value in run_ids if str(value).strip()]
        if not cleaned_run_ids:
            return 0
        query = query.where(RunLogEvent.run_id.in_(cleaned_run_ids))
    if start_date:
        query = query.where(RunLogEvent.recorded_at >= start_date)
    if end_date:
        query = query.where(RunLogEvent.recorded_at <= end_date)

    rows = session.execute(query).scalars().all()
    inserted = 0
    for row in rows:
        if materialize_token_usage_from_log_message(session=session, run_log_row=_to_normalized_row(row)):
            inserted += 1
    if inserted:
        session.flush()
    return inserted
