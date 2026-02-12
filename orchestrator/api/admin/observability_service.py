from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

from fastapi import HTTPException, status
from sqlalchemy import func, select

from orchestrator.api.schemas import (
    ObservabilityDurationStatsRead,
    PlatformObservabilityRead,
    ProjectObservabilityRead,
    TenantObservabilityRead,
)
from orchestrator.storage.models import Project, Run, Tenant

ACTIVE_RUN_STATUSES = {"queued", "running"}
STALE_STATUSES = {"queued", "running"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _duration_stats(runs: list[Run]) -> ObservabilityDurationStatsRead:
    durations: list[float] = []
    for run in runs:
        if run.started_at is None or run.finished_at is None:
            continue
        started_at = _coerce_aware(run.started_at)
        finished_at = _coerce_aware(run.finished_at)
        duration = (finished_at - started_at).total_seconds()
        if duration >= 0:
            durations.append(duration)
    if not durations:
        return ObservabilityDurationStatsRead(average_seconds=0.0, median_seconds=0.0, p95_seconds=0.0)
    ordered = sorted(durations)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    return ObservabilityDurationStatsRead(
        average_seconds=round(sum(ordered) / len(ordered), 3),
        median_seconds=round(float(median(ordered)), 3),
        p95_seconds=round(float(ordered[p95_index]), 3),
    )


def _status_counts(runs: list[Run]) -> dict[str, int]:
    counts = {
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "failed": 0,
        "blocked": 0,
    }
    for run in runs:
        if run.status in counts:
            counts[run.status] += 1
    return counts


def platform_observability(*, session) -> PlatformObservabilityRead:  # noqa: ANN001
    total_tenants = int(session.execute(select(func.count(Tenant.tenant_id))).scalar_one())
    enabled_tenants = int(
        session.execute(select(func.count(Tenant.tenant_id)).where(Tenant.is_enabled.is_(True))).scalar_one()
    )
    total_projects = int(session.execute(select(func.count(Project.project_id))).scalar_one())
    active_projects = int(
        session.execute(select(func.count(Project.project_id)).where(Project.is_archived.is_(False))).scalar_one()
    )
    runs = session.execute(select(Run)).scalars().all()
    total_runs = len(runs)
    active_runs = sum(1 for run in runs if run.status in ACTIVE_RUN_STATUSES)
    cutoff = _utcnow() - timedelta(hours=24)
    failed_runs_last_24h = sum(
        1 for run in runs if run.status == "failed" and _coerce_aware(run.created_at) >= cutoff
    )
    return PlatformObservabilityRead(
        total_tenants=total_tenants,
        enabled_tenants=enabled_tenants,
        total_projects=total_projects,
        active_projects=active_projects,
        total_runs=total_runs,
        active_runs=active_runs,
        failed_runs_last_24h=failed_runs_last_24h,
        run_duration=_duration_stats(runs),
    )


def tenant_observability(*, session, tenant_id: str) -> TenantObservabilityRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    total_projects = int(
        session.execute(select(func.count(Project.project_id)).where(Project.tenant_id == tenant_id)).scalar_one()
    )
    active_projects = int(
        session.execute(
            select(func.count(Project.project_id)).where(
                Project.tenant_id == tenant_id,
                Project.is_archived.is_(False),
            )
        ).scalar_one()
    )
    runs = session.execute(select(Run).where(Run.tenant_id == tenant_id)).scalars().all()
    counts = _status_counts(runs)
    stale_cutoff = _utcnow() - timedelta(hours=2)
    stale_runs = sum(
        1 for run in runs if run.status in STALE_STATUSES and _coerce_aware(run.created_at) <= stale_cutoff
    )
    return TenantObservabilityRead(
        tenant_id=tenant_id,
        total_projects=total_projects,
        active_projects=active_projects,
        total_runs=len(runs),
        queued_runs=counts["queued"],
        running_runs=counts["running"],
        succeeded_runs=counts["succeeded"],
        failed_runs=counts["failed"],
        blocked_runs=counts["blocked"],
        stale_runs=stale_runs,
        run_duration=_duration_stats(runs),
    )


def project_observability(*, session, tenant_id: str, project_id: str) -> ProjectObservabilityRead:  # noqa: ANN001
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    runs = session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.project_id == project_id,
        )
    ).scalars().all()
    counts = _status_counts(runs)
    stale_cutoff = _utcnow() - timedelta(hours=2)
    stale_runs = sum(
        1 for run in runs if run.status in STALE_STATUSES and _coerce_aware(run.created_at) <= stale_cutoff
    )
    return ProjectObservabilityRead(
        tenant_id=tenant_id,
        project_id=project_id,
        total_runs=len(runs),
        queued_runs=counts["queued"],
        running_runs=counts["running"],
        succeeded_runs=counts["succeeded"],
        failed_runs=counts["failed"],
        blocked_runs=counts["blocked"],
        stale_runs=stale_runs,
        run_duration=_duration_stats(runs),
    )
