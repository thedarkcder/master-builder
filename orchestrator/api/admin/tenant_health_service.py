from __future__ import annotations

from orchestrator.api.schemas import TenantHealthRead, TenantIntegrationHealthRead
from orchestrator.core.platform.operational_health_service import load_tenant_operational_health


def tenant_health(*, session, tenant_id: str) -> TenantHealthRead:  # noqa: ANN001
    snapshot = load_tenant_operational_health(session=session, tenant_id=tenant_id)
    return TenantHealthRead(
        tenant_id=snapshot.tenant_id,
        active_projects=snapshot.active_projects,
        active_agents=snapshot.active_agents,
        total_runs=snapshot.total_runs,
        failed_runs=snapshot.failed_runs,
        run_failure_rate_ratio=snapshot.run_failure_rate_ratio,
        average_task_duration_seconds=snapshot.average_task_duration_seconds,
        webhook_events_received=snapshot.webhook_events_received,
        webhook_events_failed=snapshot.webhook_events_failed,
        webhook_failure_rate_ratio=snapshot.webhook_failure_rate_ratio,
        integrations=TenantIntegrationHealthRead(
            jira_connected=snapshot.integrations.jira_connected,
            github_connected=snapshot.integrations.github_connected,
            jira_webhook_healthy=snapshot.integrations.jira_webhook_healthy,
        ),
    )
