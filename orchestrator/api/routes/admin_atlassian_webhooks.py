from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.jira_webhook_helpers import (
    jira_webhook_callback_url,
    parse_managed_webhook_ids,
)
from orchestrator.api.admin.jira_webhook_response_helpers import (
    build_jira_webhook_diagnostics as build_jira_webhook_diagnostics_impl,
)
from orchestrator.api.admin.jira_webhook_route_service import (
    get_jira_webhook_diagnostics as get_jira_webhook_diagnostics_route_impl,
    run_tenant_jira_webhook_action as run_tenant_jira_webhook_action_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import JiraWebhookActionResult, JiraWebhookDiagnosticsRead
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin
from orchestrator.storage.models import Tenant

router = APIRouter(prefix="/api/admin", tags=["admin", "atlassian-webhooks"])


@router.get(
    "/tenants/{tenant_id}/atlassian/jira/webhooks/diagnostics",
    response_model=JiraWebhookDiagnosticsRead,
)
def get_jira_webhook_diagnostics(
    tenant_id: str,
    within_minutes: int = Query(default=60, ge=1, le=1440),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookDiagnosticsRead:
    return get_jira_webhook_diagnostics_route_impl(
        session=session,
        tenant_id=tenant_id,
        within_minutes=within_minutes,
        tenant_model=Tenant,
        settings=get_settings(),
        build_jira_webhook_diagnostics_fn=build_jira_webhook_diagnostics_impl,
        jira_webhook_callback_url_fn=jira_webhook_callback_url,
        parse_managed_webhook_ids_fn=parse_managed_webhook_ids,
    )


@router.post(
    "/tenants/{tenant_id}/atlassian/jira/webhooks/provision",
    response_model=JiraWebhookActionResult,
)
def provision_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return run_tenant_jira_webhook_action_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        provision_jira_webhook_fn=deps.provision_jira_webhook,
        replace_existing=False,
    )


@router.post(
    "/tenants/{tenant_id}/atlassian/jira/webhooks/reset",
    response_model=JiraWebhookActionResult,
)
def reset_tenant_jira_webhook(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> JiraWebhookActionResult:
    return run_tenant_jira_webhook_action_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        provision_jira_webhook_fn=deps.provision_jira_webhook,
        replace_existing=True,
    )
