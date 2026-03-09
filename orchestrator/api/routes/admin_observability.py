from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from orchestrator.api.admin.agent_activity_service import list_agent_activity as list_agent_activity_impl
from orchestrator.api.admin.alert_policy_service import evaluate_alerts as evaluate_alerts_impl
from orchestrator.api.admin.observability_service import (
    platform_observability as platform_observability_impl,
    project_observability as project_observability_impl,
    tenant_observability as tenant_observability_impl,
)
from orchestrator.api.admin.project_metrics_service import (
    project_execution_metrics as project_execution_metrics_impl,
)
from orchestrator.api.admin.tenant_health_service import tenant_health as tenant_health_impl
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    AgentActivityRead,
    AlertEvaluationRead,
    PlatformObservabilityRead,
    ProjectExecutionMetricsRead,
    ProjectObservabilityRead,
    TenantHealthRead,
    TenantObservabilityRead,
)
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/agents/activity", response_model=list[AgentActivityRead])
def list_agent_activity(
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    heartbeat_timeout_seconds: int = Query(default=300, ge=1, le=86400),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[AgentActivityRead]:
    return list_agent_activity_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
    )


@router.get("/tenants/{tenant_id}/projects/{project_id}/metrics", response_model=ProjectExecutionMetricsRead)
def get_project_execution_metrics(
    tenant_id: str,
    project_id: str,
    sla_seconds: int = Query(default=1800, ge=1, le=86400),
    stale_queue_seconds: int = Query(default=7200, ge=1, le=604800),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectExecutionMetricsRead:
    return project_execution_metrics_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        sla_seconds=sla_seconds,
        stale_queue_seconds=stale_queue_seconds,
    )


@router.get("/alerts/evaluate", response_model=AlertEvaluationRead)
def evaluate_alerts(
    tenant_id: str | None = Query(default=None),
    cooldown_seconds: int = Query(default=600, ge=1, le=3600),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AlertEvaluationRead:
    return evaluate_alerts_impl(
        session=session,
        tenant_id=tenant_id,
        cooldown_seconds=cooldown_seconds,
    )


@router.get("/tenants/{tenant_id}/health", response_model=TenantHealthRead)
def get_tenant_health(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantHealthRead:
    return tenant_health_impl(
        session=session,
        tenant_id=tenant_id,
    )


@router.get("/observability/platform", response_model=PlatformObservabilityRead)
def platform_observability(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformObservabilityRead:
    return platform_observability_impl(session=session)


@router.get("/observability/tenants/{tenant_id}", response_model=TenantObservabilityRead)
def tenant_observability(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantObservabilityRead:
    return tenant_observability_impl(
        session=session,
        tenant_id=tenant_id,
    )


@router.get(
    "/observability/tenants/{tenant_id}/projects/{project_id}",
    response_model=ProjectObservabilityRead,
)
def project_observability(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectObservabilityRead:
    return project_observability_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
    )
