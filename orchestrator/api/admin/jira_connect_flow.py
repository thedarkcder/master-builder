from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import JiraConnectStart
from orchestrator.core.jira_oauth_state import (
    create_jira_oauth_state_token,
    parse_jira_oauth_state_token,
)
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.models import JiraOAuthConnection, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError

logger = logging.getLogger(__name__)


def build_jira_connect_start(
    *,
    return_to: str,
    tenant_id: str | None,
    session: Session,
    settings,
    jira_oauth_client_fn,
) -> JiraConnectStart:  # noqa: ANN001
    if return_to == "edit" and not tenant_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="tenant_id is required when return_to=edit",
        )
    if tenant_id and session.get(Tenant, tenant_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    state_token = create_jira_oauth_state_token(
        exp=expires_at,
        secret=settings.jira_oauth_state_secret,
        return_to=return_to,
        tenant_id=tenant_id,
    )
    try:
        client = jira_oauth_client_fn(session=session, settings=settings, tenant_id=tenant_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    authorize_url = client.build_authorize_url(state=state_token)
    return JiraConnectStart(authorize_url=authorize_url, expires_at=expires_at)


def handle_jira_connect_callback(
    *,
    code: str,
    state_token: str,
    session: Session,
    settings,
    jira_oauth_client_fn,
    auto_provision_jira_webhook_fn=None,
) -> str:  # noqa: ANN001
    try:
        state = parse_jira_oauth_state_token(
            token=state_token,
            secret=settings.jira_oauth_state_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    try:
        client = jira_oauth_client_fn(session=session, settings=settings, tenant_id=state.tenant_id)
        token_set = client.exchange_code(code=code)
        resources = client.list_accessible_resources(access_token=token_set.access_token)
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    if not resources:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No Jira resources were granted by OAuth",
        )

    resource = resources[0]
    now = datetime.now(timezone.utc)
    connection = JiraOAuthConnection(
        connection_id=str(uuid4()),
        account_id="unknown",
        account_email=None,
        cloud_id=resource.cloud_id,
        site_url=resource.site_url,
        scopes=token_set.scopes,
        access_token_encrypted=encrypt_value(
            plaintext=token_set.access_token,
            encryption_key=settings.secrets_encryption_key,
        ),
        refresh_token_encrypted=encrypt_value(
            plaintext=token_set.refresh_token,
            encryption_key=settings.secrets_encryption_key,
        ),
        access_token_expires_at=token_set.expires_at,
        created_at=now,
        updated_at=now,
    )
    session.add(connection)
    session.flush()

    auto_provision_state = "skipped"
    if state.return_to == "edit" and state.tenant_id:
        tenant = session.get(Tenant, state.tenant_id)
        if tenant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
        jira_config = dict(tenant.jira_config)
        jira_config["connection_id"] = connection.connection_id
        tenant.jira_config = jira_config
        tenant.updated_at = now

    session.commit()

    if state.return_to == "edit" and state.tenant_id and auto_provision_jira_webhook_fn is not None:
        tenant = session.get(Tenant, state.tenant_id)
        if tenant is not None:
            try:
                provision_result = auto_provision_jira_webhook_fn(
                    session=session,
                    tenant=tenant,
                    settings=settings,
                )
                auto_provision_state = "ok" if bool(getattr(provision_result, "ok", False)) else "failed"
            except Exception:
                auto_provision_state = "failed"
                logger.exception(
                    "jira_connect_callback_webhook_autoprovision_failed tenant_id=%s",
                    state.tenant_id,
                )

    if state.return_to == "edit" and state.tenant_id:
        webhook_query = f"&jira_webhook={quote(auto_provision_state, safe='')}"
        return (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/{quote(state.tenant_id, safe='')}/edit"
            f"?jira_oauth=success&jira_connection_id={quote(connection.connection_id, safe='')}{webhook_query}"
        )
    return (
        f"{settings.admin_ui_base_url.rstrip('/')}/tenants/new"
        f"?jira_oauth=success&jira_connection_id={quote(connection.connection_id, safe='')}"
    )
