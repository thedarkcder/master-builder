from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import ceil
from statistics import median

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.schemas import ProjectExecutionMetricsRead
from orchestrator.storage.models import Project, Run


def _to_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def project_execution_metrics(
    *,
    session,
    tenant_id: str,
    project_id: str,
    sla_seconds: int = 1800,
    stale_queue_seconds: int = 7200,
) -> ProjectExecutionMetricsRead:  # noqa: ANN001
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    runs = session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.project_id == project_id,
        )
    ).scalars().all()

    started = sum(1 for run in runs if run.started_at is not None)
    completed = sum(1 for run in runs if run.status in {"succeeded", "failed", "blocked", "cancelled"})
    failed = sum(1 for run in runs if run.status == "failed")
    blocked = sum(1 for run in runs if run.status == "blocked")
    queue_runs = [run for run in runs if run.status == "queued"]
    queue_length = len(queue_runs)

    terminal = [run for run in runs if run.status in {"succeeded", "failed", "blocked", "cancelled"}]
    success_rate = 0.0
    if terminal:
        succeeded = sum(1 for run in terminal if run.status == "succeeded")
        success_rate = succeeded / len(terminal)

    durations: list[float] = []
    for run in runs:
        started_at = _to_aware(run.started_at)
        finished_at = _to_aware(run.finished_at)
        if started_at is None or finished_at is None:
            continue
        seconds = (finished_at - started_at).total_seconds()
        if seconds >= 0:
            durations.append(seconds)

    average_duration = 0.0
    median_duration = 0.0
    p95_duration = 0.0
    if durations:
        ordered = sorted(durations)
        average_duration = round(sum(ordered) / len(ordered), 3)
        median_duration = round(float(median(ordered)), 3)
        p95_index = max(0, ceil(len(ordered) * 0.95) - 1)
        p95_duration = round(float(ordered[p95_index]), 3)

    now = datetime.now(timezone.utc)
    queue_times = []
    stale_cutoff = now - timedelta(seconds=max(1, stale_queue_seconds))
    stale_queued = 0
    for run in queue_runs:
        created_at = _to_aware(run.created_at)
        if created_at is None:
            continue
        queue_times.append(max(0.0, (now - created_at).total_seconds()))
        if created_at <= stale_cutoff:
            stale_queued += 1
    avg_queue_time = 0.0 if not queue_times else round(sum(queue_times) / len(queue_times), 3)

    sla_breaches = sum(1 for duration in durations if duration > max(1, sla_seconds))

    return ProjectExecutionMetricsRead(
        tenant_id=tenant_id,
        project_id=project_id,
        tasks_started=started,
        tasks_completed=completed,
        tasks_failed=failed,
        tasks_blocked=blocked,
        success_rate_ratio=round(max(0.0, success_rate), 6),
        average_duration_seconds=average_duration,
        median_duration_seconds=median_duration,
        p95_duration_seconds=p95_duration,
        queue_length=queue_length,
        average_time_in_queue_seconds=avg_queue_time,
        stale_queued_tasks=stale_queued,
        sla_breaches=sla_breaches,
    )
