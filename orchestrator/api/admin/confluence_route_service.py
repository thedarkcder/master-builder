from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.core.platform.admin_notifications import (
    ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
    AdminNotificationDraft,
    AdminNotificationScope,
    emit_admin_notification,
    resolve_admin_notification_state,
)
from orchestrator.tools.atlassian_oauth_models import (
    AtlassianOAuthAuthRequiredError,
    AtlassianOAuthError,
    AtlassianOAuthHttpError,
    ConfluencePage,
)

CONFLUENCE_SPACE_READ_SCOPE = "read:space:confluence"
CONFLUENCE_PAGE_READ_SCOPE = "read:page:confluence"


def _requires_reauth(exc: Exception) -> bool:
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


def _notification_scope(*, tenant_id: str, connection) -> AdminNotificationScope:  # noqa: ANN001
    return AdminNotificationScope(
        scope_type="jira_connection",
        scope_id=connection.connection_id,
        tenant_id=tenant_id,
    )


def _emit_reauth_notification(*, session, tenant_id: str, connection) -> None:  # noqa: ANN001
    emit_admin_notification(
        session=session,
        notification=AdminNotificationDraft(
            scope=_notification_scope(tenant_id=tenant_id, connection=connection),
            source="atlassian_oauth",
            kind=ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
            detail=(
                "Stored Atlassian credentials are no longer valid. Reconnect Atlassian from tenant settings to "
                "restore Jira and Confluence administration."
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


def _resolve_connection_for_tenant(*, session, tenant_id: str, tenant_model, resolve_tenant_atlassian_connection_fn):  # noqa: ANN001
    tenant = session.get(tenant_model, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    connection = resolve_tenant_atlassian_connection_fn(session=session, tenant=tenant)
    return tenant, connection


def _require_scopes(*, connection, scopes: set[str]) -> None:  # noqa: ANN001
    granted = {str(scope or "").strip() for scope in getattr(connection, "scopes", []) if str(scope or "").strip()}
    missing = sorted(scopes - granted)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Connected Atlassian OAuth grant is missing required Confluence scopes: "
                + ", ".join(missing)
                + ". Reconnect Atlassian with Confluence access."
            ),
        )


def list_confluence_spaces_for_tenant(
    *,
    session,
    tenant_id: str,
    tenant_model,
    settings,
    resolve_tenant_atlassian_connection_fn,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
):  # noqa: ANN001
    tenant, connection = _resolve_connection_for_tenant(
        session=session,
        tenant_id=tenant_id,
        tenant_model=tenant_model,
        resolve_tenant_atlassian_connection_fn=resolve_tenant_atlassian_connection_fn,
    )
    _require_scopes(connection=connection, scopes={CONFLUENCE_SPACE_READ_SCOPE})
    try:
        access_token = refresh_atlassian_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
        )
    except (ValueError, AtlassianOAuthError) as exc:
        if _requires_reauth(exc):
            _emit_reauth_notification(session=session, tenant_id=tenant.tenant_id, connection=connection)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Atlassian connection requires reauthentication.") from exc
        raise
    client = atlassian_oauth_client_fn(session=session, settings=settings)
    spaces = client.list_confluence_spaces(
        access_token=access_token,
        cloud_id=connection.cloud_id,
    )
    resolve_admin_notification_state(
        session=session,
        scope=_notification_scope(tenant_id=tenant.tenant_id, connection=connection),
        kind=ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
        dedupe_key="reauth_required",
    )
    session.commit()
    return {
        "items": [
            {"space_id": space.space_id, "key": space.key, "name": space.name}
            for space in sorted(spaces, key=lambda item: (item.name.casefold(), item.key.casefold()))
        ],
        "create_space_url": f"{str(connection.site_url or '').rstrip('/')}/wiki/spaces/create",
    }


def list_confluence_pages_for_tenant(
    *,
    session,
    tenant_id: str,
    space_key: str,
    selected_page_id: str | None,
    tenant_model,
    settings,
    resolve_tenant_atlassian_connection_fn,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
):  # noqa: ANN001
    tenant, connection = _resolve_connection_for_tenant(
        session=session,
        tenant_id=tenant_id,
        tenant_model=tenant_model,
        resolve_tenant_atlassian_connection_fn=resolve_tenant_atlassian_connection_fn,
    )
    _require_scopes(connection=connection, scopes={CONFLUENCE_SPACE_READ_SCOPE, CONFLUENCE_PAGE_READ_SCOPE})
    try:
        access_token = refresh_atlassian_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
        )
    except (ValueError, AtlassianOAuthError) as exc:
        if _requires_reauth(exc):
            _emit_reauth_notification(session=session, tenant_id=tenant.tenant_id, connection=connection)
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Atlassian connection requires reauthentication.") from exc
        raise
    client = atlassian_oauth_client_fn(session=session, settings=settings)
    space = client.get_confluence_space_by_key(
        access_token=access_token,
        cloud_id=connection.cloud_id,
        space_key=space_key,
    )
    pages = client.list_confluence_pages(
        access_token=access_token,
        cloud_id=connection.cloud_id,
        site_url=connection.site_url,
        space_id=space.space_id,
    )
    normalized_selected_page_id = str(selected_page_id or "").strip()
    if normalized_selected_page_id and all(page.page_id != normalized_selected_page_id for page in pages):
        selected_page = client.get_confluence_page(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            site_url=connection.site_url,
            page_id=normalized_selected_page_id,
        )
        pages.append(selected_page)
    resolve_admin_notification_state(
        session=session,
        scope=_notification_scope(tenant_id=tenant.tenant_id, connection=connection),
        kind=ADMIN_NOTIFICATION_KIND_JIRA_CONNECTION_REAUTH_REQUIRED,
        dedupe_key="reauth_required",
    )
    session.commit()
    deduped_pages: dict[str, ConfluencePage] = {}
    for page in pages:
        deduped_pages[page.page_id] = page
    return [
        {"page_id": page.page_id, "title": page.title, "webui_url": page.webui_url}
        for page in sorted(deduped_pages.values(), key=lambda item: item.title.casefold())
    ]
