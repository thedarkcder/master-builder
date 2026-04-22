from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_ATLASSIAN_OAUTH_CLIENT_ID_REF,
    PLATFORM_SECRET_ATLASSIAN_OAUTH_CLIENT_SECRET_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.storage.models import AtlassianOAuthConnection
from orchestrator.tools.atlassian_oauth import AtlassianOAuthClient, AtlassianOAuthClientConfig


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


def atlassian_oauth_client(
    *,
    session: Session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> AtlassianOAuthClient:  # noqa: ANN001
    client_id = resolve_secret_ref(
        session,
        ref_name=PLATFORM_SECRET_ATLASSIAN_OAUTH_CLIENT_ID_REF,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    client_secret = resolve_secret_ref(
        session,
        ref_name=PLATFORM_SECRET_ATLASSIAN_OAUTH_CLIENT_SECRET_REF,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    redirect_uri = f"{settings.public_api_base_url.rstrip('/')}/api/admin/atlassian/connect/callback"
    return AtlassianOAuthClient(
        AtlassianOAuthClientConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    )


def refresh_atlassian_connection_tokens(
    session: Session,
    *,
    connection: AtlassianOAuthConnection,
    settings,
    tenant_id: str | None = None,
    atlassian_oauth_client_fn=atlassian_oauth_client,
) -> str:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    token_expires_at = connection.access_token_expires_at
    if token_expires_at.tzinfo is None:
        token_expires_at = token_expires_at.replace(tzinfo=timezone.utc)
    if token_expires_at - now > timedelta(seconds=60):
        return decrypt_value(
            ciphertext=connection.access_token_encrypted,
            encryption_key=settings.secrets_encryption_key,
        )

    client = atlassian_oauth_client_fn(session=session, settings=settings, tenant_id=tenant_id)
    refresh_token = decrypt_value(
        ciphertext=connection.refresh_token_encrypted,
        encryption_key=settings.secrets_encryption_key,
    )
    token_set = client.refresh_tokens(refresh_token=refresh_token)
    connection.access_token_encrypted = encrypt_value(
        plaintext=token_set.access_token,
        encryption_key=settings.secrets_encryption_key,
    )
    connection.refresh_token_encrypted = encrypt_value(
        plaintext=token_set.refresh_token,
        encryption_key=settings.secrets_encryption_key,
    )
    connection.access_token_expires_at = token_set.expires_at
    connection.scopes = token_set.scopes
    connection.updated_at = now
    session.commit()
    return token_set.access_token
