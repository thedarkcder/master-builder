from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_JIRA_OAUTH_CLIENT_ID_REF,
    PLATFORM_SECRET_JIRA_OAUTH_CLIENT_SECRET_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.secret_crypto import (
    decrypt_secret_value,
    encrypt_secret_value,
    jira_oauth_token_crypto_context,
)
from orchestrator.storage.models import JiraOAuthConnection
from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig


def resolve_secret_ref(
    session: Session,
    *,
    ref_name: str,
    settings,
    tenant_id: str | None = None,  # noqa: ARG001
    project_id: str | None = None,  # noqa: ARG001
) -> str:  # noqa: ANN001
    value = resolve_platform_secret_ref(
        session,
        secret_ref=ref_name,
        encryption_key=settings.secrets_encryption_key,
    )
    if not value:
        raise ValueError(f"Missing secret value for ref '{ref_name}'")
    return value


def jira_oauth_client(
    *,
    session: Session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> JiraOAuthClient:  # noqa: ANN001
    client_id = resolve_secret_ref(
        session,
        ref_name=PLATFORM_SECRET_JIRA_OAUTH_CLIENT_ID_REF,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    client_secret = resolve_secret_ref(
        session,
        ref_name=PLATFORM_SECRET_JIRA_OAUTH_CLIENT_SECRET_REF,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    redirect_uri = f"{settings.public_api_base_url.rstrip('/')}/api/admin/jira/connect/callback"
    return JiraOAuthClient(
        JiraOAuthClientConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    )


def refresh_jira_connection_tokens(
    session: Session,
    *,
    connection: JiraOAuthConnection,
    settings,
    tenant_id: str | None = None,
    jira_oauth_client_fn=jira_oauth_client,
) -> str:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    token_expires_at = connection.access_token_expires_at
    if token_expires_at.tzinfo is None:
        token_expires_at = token_expires_at.replace(tzinfo=timezone.utc)
    if token_expires_at - now > timedelta(seconds=60):
        return decrypt_secret_value(
            ciphertext=connection.access_token_encrypted,
            settings=settings,
            encryption_key=settings.secrets_encryption_key,
            context=jira_oauth_token_crypto_context(
                connection_id=connection.connection_id,
                token_field="access_token",
            ),
        )

    client = jira_oauth_client_fn(session=session, settings=settings, tenant_id=tenant_id)
    refresh_token = decrypt_secret_value(
        ciphertext=connection.refresh_token_encrypted,
        settings=settings,
        encryption_key=settings.secrets_encryption_key,
        context=jira_oauth_token_crypto_context(
            connection_id=connection.connection_id,
            token_field="refresh_token",
        ),
    )
    token_set = client.refresh_tokens(refresh_token=refresh_token)
    connection.access_token_encrypted = encrypt_secret_value(
        plaintext=token_set.access_token,
        settings=settings,
        encryption_key=settings.secrets_encryption_key,
        context=jira_oauth_token_crypto_context(
            connection_id=connection.connection_id,
            token_field="access_token",
        ),
    )
    connection.refresh_token_encrypted = encrypt_secret_value(
        plaintext=token_set.refresh_token,
        settings=settings,
        encryption_key=settings.secrets_encryption_key,
        context=jira_oauth_token_crypto_context(
            connection_id=connection.connection_id,
            token_field="refresh_token",
        ),
    )
    connection.access_token_expires_at = token_set.expires_at
    connection.scopes = token_set.scopes
    connection.updated_at = now
    session.commit()
    return token_set.access_token
