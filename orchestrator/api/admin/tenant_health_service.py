from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.schemas import TenantHealthRead, TenantIntegrationHealthRead
from orchestrator.core.webhook_health import webhook_health_tracker
from orchestrator.storage.models import Project, Run, Tenant

ACTIVE_RUN_STATUSES = {"queued", "running"}


def _to_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _is_recent_timestamp(raw: str | None, *, within_hours: int) -> bool:
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return False
    parsed_aware = _to_aware(parsed)
    if parsed_aware is None:
        return False
    return parsed_aware >= datetime.now(timezone.utc) - timedelta(hours=within_hours)


def tenant_health(*, session, tenant_id: str) -> TenantHealthRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    active_projects = len(
        session.execute(
            select(Project).where(
                Project.tenant_id == tenant_id,
                Project.is_archived.is_(False),
            )
        )
        .scalars()
        .all()
    )

    runs = session.execute(select(Run).where(Run.tenant_id == tenant_id)).scalars().all()
    total_runs = len(runs)
    failed_runs = sum(1 for run in runs if run.status == "failed")
    active_agents = sum(1 for run in runs if run.status in ACTIVE_RUN_STATUSES)

    run_failure_rate_ratio = 0.0 if total_runs <= 0 else failed_runs / total_runs

    durations: list[float] = []
    for run in runs:
        started = _to_aware(run.started_at)
        finished = _to_aware(run.finished_at)
        if started is None or finished is None:
            continue
        seconds = (finished - started).total_seconds()
        if seconds >= 0:
            durations.append(seconds)
    average_duration = 0.0 if not durations else round(sum(durations) / len(durations), 3)

    webhook_rollup = webhook_health_tracker.rollup(tenant_id=tenant_id)
    jira_config = dict(tenant.jira_config or {})
    github_config = dict(tenant.github_config or {})
    integrations = TenantIntegrationHealthRead(
        jira_connected=bool(str(jira_config.get("connection_id") or "").strip()),
        github_connected=bool(str(github_config.get("installation_id") or "").strip()),
        jira_webhook_healthy=(
            _is_recent_timestamp(jira_config.get("webhook_last_received_at"), within_hours=24)
            and not bool(str(jira_config.get("webhook_last_error") or "").strip())
        ),
    )

    return TenantHealthRead(
        tenant_id=tenant_id,
        active_projects=active_projects,
        active_agents=active_agents,
        total_runs=total_runs,
        failed_runs=failed_runs,
        run_failure_rate_ratio=round(max(0.0, run_failure_rate_ratio), 6),
        average_task_duration_seconds=average_duration,
        webhook_events_received=int(webhook_rollup["received_total"]),
        webhook_events_failed=int(webhook_rollup["failed_total"]),
        webhook_failure_rate_ratio=webhook_rollup["failure_rate_ratio"],
        integrations=integrations,
    )

