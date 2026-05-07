from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse


def get_jira_webhook_diagnostics(
    *,
    session,
    tenant_id: str,
    within_minutes: int,
    tenant_model,
    settings,
    build_jira_webhook_diagnostics_fn,
    jira_webhook_callback_url_fn,
    parse_managed_webhook_ids_fn,
):  # noqa: ANN001
    tenant = session.get(tenant_model, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return build_jira_webhook_diagnostics_fn(
        tenant_id=tenant_id,
        within_minutes=within_minutes,
        jira_config=dict(tenant.jira_config),
        settings=settings,
        jira_webhook_callback_url_fn=jira_webhook_callback_url_fn,
        parse_managed_webhook_ids_fn=parse_managed_webhook_ids_fn,
    )


def run_tenant_jira_webhook_action(
    *,
    session,
    tenant_id: str,
    tenant_model,
    settings,
    provision_jira_webhook_fn,
    replace_existing: bool,
):  # noqa: ANN001
    tenant = session.get(tenant_model, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    result = provision_jira_webhook_fn(
        session=session,
        tenant=tenant,
        settings=settings,
        replace_existing=replace_existing,
    )
    if not result.ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=result.details)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=result.model_dump(),
    )
