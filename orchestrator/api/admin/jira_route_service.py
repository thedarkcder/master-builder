from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from orchestrator.api.schemas import JiraProjectRead


def list_jira_projects_for_connection(
    *,
    session,
    connection_id: str,
    jira_oauth_connection_model,
    settings,
    refresh_jira_connection_tokens_fn,
    jira_oauth_client_fn,
):  # noqa: ANN001
    connection = session.get(jira_oauth_connection_model, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Jira connection not found")

    access_token = refresh_jira_connection_tokens_fn(
        session,
        connection=connection,
        settings=settings,
    )
    client = jira_oauth_client_fn(session=session, settings=settings)
    projects = client.list_projects(access_token=access_token, cloud_id=connection.cloud_id)
    return [JiraProjectRead(key=project.key, name=project.name) for project in projects]


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
    jira_webhook_action_status_code_fn,
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
    return JSONResponse(
        status_code=jira_webhook_action_status_code_fn(result),
        content=result.model_dump(),
    )
