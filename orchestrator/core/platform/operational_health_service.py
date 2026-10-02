from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from orchestrator.core.decision.types import (
    JiraConfigKey,
    jira_config_text,
    jira_config_project_keys,
    tenant_jira_ready_statuses,
)
from orchestrator.core.webhooks.health import webhook_health_tracker
from orchestrator.storage.models import Project, Run, Tenant

ACTIVE_RUN_STATUSES = {"queued", "dispatching", "running"}


@dataclass(frozen=True)
class TenantIntegrationSnapshot:
    jira_connected: bool
    github_connected: bool
    jira_webhook_healthy: bool
    atlassian_connection_id: str | None
    github_installation_id: str | None
    jira_project_keys: list[str]
    jira_ready_statuses: list[str]
    jira_webhook_last_received_at: str | None
    jira_webhook_last_error: str | None


@dataclass(frozen=True)
class TenantOperationalHealthSnapshot:
    tenant_id: str
    active_projects: int
    active_agents: int
    total_runs: int
    failed_runs: int
    run_failure_rate_ratio: float
    average_task_duration_seconds: float
    webhook_events_received: int
    webhook_events_failed: int
    webhook_failure_rate_ratio: float
    integrations: TenantIntegrationSnapshot


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


def tenant_integration_snapshot(*, tenant: Tenant) -> TenantIntegrationSnapshot:
    jira_config = dict(tenant.jira_config or {})
    github_config = dict(tenant.github_config or {})
    atlassian_connection_id = jira_config_text(
        jira_config=jira_config, key=JiraConfigKey.CONNECTION_ID
    )
    github_installation_id = (
        str(github_config.get("installation_id") or "").strip() or None
    )
    jira_webhook_last_received_at = (
        str(jira_config.get("webhook_last_received_at") or "").strip() or None
    )
    jira_webhook_last_error = (
        str(jira_config.get("webhook_last_error") or "").strip() or None
    )
    return TenantIntegrationSnapshot(
        jira_connected=bool(atlassian_connection_id),
        github_connected=bool(github_installation_id),
        jira_webhook_healthy=(
            _is_recent_timestamp(jira_webhook_last_received_at, within_hours=24)
            and not bool(jira_webhook_last_error)
        ),
        atlassian_connection_id=atlassian_connection_id,
        github_installation_id=github_installation_id,
        jira_project_keys=list(jira_config_project_keys(jira_config=jira_config)),
        jira_ready_statuses=list(tenant_jira_ready_statuses(tenant))
        or ["Ready for Agent"],
        jira_webhook_last_received_at=jira_webhook_last_received_at,
        jira_webhook_last_error=jira_webhook_last_error,
    )


def load_tenant_operational_health(
    *,
    session: Session,
    tenant_id: str,
) -> TenantOperationalHealthSnapshot:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    return _tenant_operational_health_for_tenant(session=session, tenant=tenant)


def list_enabled_tenant_operational_health(
    *,
    session: Session,
    tenant_id: str | None = None,
) -> list[TenantOperationalHealthSnapshot]:
    query = select(Tenant).where(Tenant.is_enabled.is_(True))
    if tenant_id:
        query = query.where(Tenant.tenant_id == tenant_id)
    tenants = session.execute(query.order_by(Tenant.tenant_id.asc())).scalars().all()
    return [
        _tenant_operational_health_for_tenant(session=session, tenant=tenant)
        for tenant in tenants
    ]


def _tenant_operational_health_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
) -> TenantOperationalHealthSnapshot:
    active_projects = int(
        session.execute(
            select(func.count(Project.project_id)).where(
                Project.tenant_id == tenant.tenant_id,
                Project.is_archived.is_(False),
            )
        ).scalar_one()
    )
    total_runs, failed_runs, active_agents = session.execute(
        select(
            func.count(Run.run_id),
            func.coalesce(func.sum(case((Run.status == "failed", 1), else_=0)), 0),
            func.coalesce(
                func.sum(case((Run.status.in_(ACTIVE_RUN_STATUSES), 1), else_=0)), 0
            ),
        ).where(Run.tenant_id == tenant.tenant_id)
    ).one()
    total_runs = int(total_runs or 0)
    failed_runs = int(failed_runs or 0)
    active_agents = int(active_agents or 0)
    run_failure_rate_ratio = 0.0 if total_runs <= 0 else failed_runs / total_runs
    duration_seconds_expr = (
        (func.julianday(Run.finished_at) - func.julianday(Run.started_at)) * 86400.0
        if session.bind is not None and session.bind.dialect.name == "sqlite"
        else func.extract("epoch", Run.finished_at - Run.started_at)
    )
    average_duration_value = session.execute(
        select(func.avg(duration_seconds_expr)).where(
            Run.tenant_id == tenant.tenant_id,
            Run.started_at.is_not(None),
            Run.finished_at.is_not(None),
            Run.finished_at >= Run.started_at,
        )
    ).scalar_one()
    average_duration = (
        0.0
        if average_duration_value is None
        else round(float(average_duration_value), 3)
    )
    webhook_rollup = webhook_health_tracker.rollup(tenant_id=tenant.tenant_id)
    integrations = tenant_integration_snapshot(tenant=tenant)
    return TenantOperationalHealthSnapshot(
        tenant_id=tenant.tenant_id,
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
