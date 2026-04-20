from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from orchestrator.api.admin.audit_export_service import iter_audit_event_export
from orchestrator.api.admin.agent_activity_service import list_agent_activity as list_agent_activity_impl
from orchestrator.api.admin.alert_policy_service import evaluate_alerts as evaluate_alerts_impl
from orchestrator.api.admin.notifications_service import list_tenant_notifications as list_tenant_notifications_impl
from orchestrator.api.admin.observability_service import (
    platform_observability as platform_observability_impl,
    project_observability as project_observability_impl,
    tenant_observability as tenant_observability_impl,
)
from orchestrator.api.admin.platform_status_service import platform_status as platform_status_impl
from orchestrator.api.admin.worker_runtime_auth_service import (
    get_worker_runtime_auth_request as get_worker_runtime_auth_request_impl,
    start_worker_runtime_auth_request as start_worker_runtime_auth_request_impl,
)
from orchestrator.api.admin.webhook_queue_service import list_webhook_queue_jobs as list_webhook_queue_jobs_impl
from orchestrator.api.admin.project_metrics_service import (
    project_execution_metrics as project_execution_metrics_impl,
)
from orchestrator.api.admin.tenant_health_service import tenant_health as tenant_health_impl
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    AuditEventExportRequest,
    AgentActivityRead,
    AlertEvaluationRead,
    AdminNotificationListRead,
    KnowledgeJiraSyncRuntimeRead,
    PlatformStatusRead,
    PlatformObservabilityRead,
    ProjectExecutionMetricsRead,
    ProjectObservabilityRead,
    TenantHealthRead,
    TenantObservabilityRead,
    WebhookQueueJobPageRead,
    WorkerRuntimeAuthRequestRead,
)
from orchestrator.core.knowledge_jira_sync_runtime import get_knowledge_jira_sync_runtime_status
from orchestrator.core.observability_policy import normalize_tenant_observability_policy
from orchestrator.core.config import get_settings
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_authenticated_principal,
    require_tenant_workspace_access,
)
from orchestrator.storage.models import Tenant

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


@router.get("/tenants/{tenant_id}/notifications", response_model=AdminNotificationListRead)
def list_tenant_notifications(
    tenant_id: str,
    status_filter: str | None = Query(default="open", alias="status"),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> AdminNotificationListRead:
    return list_tenant_notifications_impl(
        session=session,
        tenant_id=tenant_id,
        status_filter=status_filter,
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


@router.get("/observability/webhook-jobs", response_model=WebhookQueueJobPageRead)
def list_webhook_queue_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    transport: str | None = Query(default=None),
    tenant_id: str | None = Query(default=None),
    project_id: str | None = Query(default=None),
    subject_key: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> WebhookQueueJobPageRead:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    if not normalized_tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required",
        )
    if not normalized_project_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="project_id is required",
        )
    if not principal.is_platform_super_admin:
        require_tenant_workspace_access(principal=principal, tenant_id=normalized_tenant_id)
    return list_webhook_queue_jobs_impl(
        session=session,
        status_filter=status_filter,
        transport=transport,
        tenant_id=normalized_tenant_id,
        project_id=normalized_project_id,
        subject_key=subject_key,
        limit=limit,
        offset=offset,
    )


@router.get("/status", response_model=PlatformStatusRead)
def platform_status(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformStatusRead:
    return platform_status_impl(session=session, settings=get_settings())


@router.post(
    "/workers/{service_instance_id}/runtime-dependencies/{runtime_kind}/login-session",
    response_model=WorkerRuntimeAuthRequestRead,
    status_code=status.HTTP_202_ACCEPTED,
)
def start_worker_runtime_login_session(
    service_instance_id: str,
    runtime_kind: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WorkerRuntimeAuthRequestRead:
    return start_worker_runtime_auth_request_impl(
        session=session,
        service_instance_id=service_instance_id,
        runtime_kind=runtime_kind,
    )


@router.get(
    "/workers/runtime-auth-requests/{request_id}",
    response_model=WorkerRuntimeAuthRequestRead,
)
def get_worker_runtime_auth_request(
    request_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> WorkerRuntimeAuthRequestRead:
    return get_worker_runtime_auth_request_impl(
        session=session,
        request_id=request_id,
    )


@router.get("/observability/knowledge-jira-sync", response_model=KnowledgeJiraSyncRuntimeRead)
def knowledge_jira_sync_runtime_status(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> KnowledgeJiraSyncRuntimeRead:
    return KnowledgeJiraSyncRuntimeRead.model_validate(
        get_knowledge_jira_sync_runtime_status(session=session),
        from_attributes=True,
    )


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


@router.post("/audit/export")
def export_audit_events(
    payload: AuditEventExportRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    if not principal.is_platform_super_admin:
        require_tenant_workspace_access(principal=principal, tenant_id=payload.tenant_id)
    tenant = session.get(Tenant, payload.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    observability_policy = normalize_tenant_observability_policy(tenant.policy_config)
    if not observability_policy.audit_export_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Audit export is disabled for this tenant",
        )
    filename_parts = ["audit-events", payload.tenant_id]
    if payload.execution_id:
        filename_parts.append(payload.execution_id)
    headers = {
        "Content-Disposition": f"attachment; filename=\"{'-'.join(filename_parts)}.ndjson\"",
    }
    return StreamingResponse(
        iter_audit_event_export(session=session, request=payload),
        media_type="application/x-ndjson",
        headers=headers,
    )
