from __future__ import annotations

from fastapi import HTTPException, status
from fastapi.responses import JSONResponse

from orchestrator.api.schemas import JiraProjectRead
from orchestrator.core.admin_notifications import (
    ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
    AdminNotificationDraft,
    AdminNotificationScope,
    emit_admin_notification,
    resolve_admin_notification_state,
)
from orchestrator.tools.atlassian_oauth_models import AtlassianOAuthAuthRequiredError, AtlassianOAuthError, AtlassianOAuthHttpError


def _is_jira_reauth_required(exc: Exception) -> bool:
    if isinstance(exc, AtlassianOAuthAuthRequiredError):
        return True
    if isinstance(exc, AtlassianOAuthHttpError):
        return exc.status_code in {401, 403}
    if isinstance(exc, AtlassianOAuthError):
        message = str(exc).lower()
        return (
            "refresh_token is invalid" in message
            or "unauthorized_client" in message
            or "(401)" in message
            or "(403)" in message
        )
    return False


def list_jira_projects_for_connection(
    *,
    session,
    connection_id: str,
    atlassian_oauth_connection_model,
    settings,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
):  # noqa: ANN001
    connection = session.get(atlassian_oauth_connection_model, connection_id)
    if connection is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atlassian connection not found")

    notification_scope = AdminNotificationScope(
        scope_type="jira_connection",
        scope_id=connection.connection_id,
    )
    try:
        access_token = refresh_atlassian_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
        )
    except (ValueError, AtlassianOAuthError) as exc:
        if _is_jira_reauth_required(exc):
            emit_admin_notification(
                session=session,
                notification=AdminNotificationDraft(
                    scope=notification_scope,
                    source="atlassian_oauth",
                    kind=ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
                    detail=(
                        "Stored Atlassian credentials are no longer valid. Reconnect Atlassian from tenant settings to "
                        "restore project loading, issue sync, and webhook administration."
                    ),
                    dedupe_key="reauth_required",
                    context={
                        "connection_id": connection.connection_id,
                        "site_url": connection.site_url,
                        "failure_category": "invalid_refresh_token",
                    },
                ),
            )
            session.commit()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Atlassian connection requires reauthentication.",
            ) from exc
        raise
    client = atlassian_oauth_client_fn(session=session, settings=settings)
    projects = client.list_projects(access_token=access_token, cloud_id=connection.cloud_id)
    resolve_admin_notification_state(
        session=session,
        scope=notification_scope,
        kind=ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
        dedupe_key="reauth_required",
    )
    session.commit()
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
