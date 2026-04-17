from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select

from orchestrator.api.schemas import WebhookQueueJobPageRead, WebhookQueueJobRead, WebhookQueueSummaryRead
from orchestrator.storage.models import WebhookJob

_VALID_JOB_STATUSES = {"pending", "processing", "failed", "done"}


def _normalized_string(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _base_filters(
    *,
    transport: str | None,
    tenant_id: str | None,
    project_id: str | None,
    subject_key: str | None,
) -> list[object]:
    filters: list[object] = []
    normalized_transport = _normalized_string(transport)
    normalized_tenant = _normalized_string(tenant_id)
    normalized_project = _normalized_string(project_id)
    normalized_subject = _normalized_string(subject_key)
    if normalized_transport is not None:
        filters.append(WebhookJob.transport == normalized_transport)
    if normalized_tenant is not None:
        filters.append(WebhookJob.tenant_id == normalized_tenant)
    if normalized_project is not None:
        filters.append(WebhookJob.project_id == normalized_project)
    if normalized_subject is not None:
        filters.append(WebhookJob.subject_key.contains(normalized_subject))
    return filters


def _count_by_status(*, session, filters: Iterable[object]) -> dict[str, int]:  # noqa: ANN001
    rows = session.execute(
        select(WebhookJob.status, func.count(WebhookJob.job_id))
        .where(*filters)
        .group_by(WebhookJob.status)
    ).all()
    counts = {str(status): int(count or 0) for status, count in rows}
    return counts


def list_webhook_queue_jobs(
    *,
    session,
    status_filter: str | None,
    transport: str | None,
    tenant_id: str | None,
    project_id: str | None,
    subject_key: str | None,
    limit: int,
    offset: int,
) -> WebhookQueueJobPageRead:  # noqa: ANN001
    normalized_status = _normalized_string(status_filter)
    filters = _base_filters(
        transport=transport,
        tenant_id=tenant_id,
        project_id=project_id,
        subject_key=subject_key,
    )
    if normalized_status in _VALID_JOB_STATUSES:
        filters.append(WebhookJob.status == normalized_status)

    total = int(
        session.execute(
            select(func.count(WebhookJob.job_id)).where(*filters)
        ).scalar_one()
    )
    rows = session.execute(
        select(WebhookJob)
        .where(*filters)
        .order_by(WebhookJob.created_at.desc(), WebhookJob.job_id.desc())
        .limit(limit)
        .offset(offset)
    ).scalars().all()

    summary_filters = _base_filters(
        transport=transport,
        tenant_id=tenant_id,
        project_id=project_id,
        subject_key=subject_key,
    )
    counts = _count_by_status(session=session, filters=summary_filters)
    summary = WebhookQueueSummaryRead(
        pending_count=int(counts.get("pending", 0)),
        processing_count=int(counts.get("processing", 0)),
        failed_count=int(counts.get("failed", 0)),
        done_count=int(counts.get("done", 0)),
    )
    items = [
        WebhookQueueJobRead(
            job_id=row.job_id,
            transport=row.transport,
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            subject_key=row.subject_key,
            related_run_id=_normalized_string((row.context_json or {}).get("related_run_id")),
            dedupe_key=row.dedupe_key,
            request_id=row.request_id,
            event_type=row.event_type,
            status=row.status,
            lease_expires_at=row.lease_expires_at,
            available_at=row.available_at,
            attempt_count=int(row.attempt_count or 0),
            last_error=row.last_error,
            created_at=row.created_at,
            updated_at=row.updated_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
        for row in rows
    ]
    return WebhookQueueJobPageRead(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
        summary=summary,
    )
