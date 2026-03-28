from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, TypeVar

from sqlalchemy.orm import Session

from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_JIRA_OAUTH_CLIENT_ID_REF,
    PLATFORM_SECRET_JIRA_OAUTH_CLIENT_SECRET_REF,
    resolve_platform_secret_ref,
)
from orchestrator.storage.models import JiraOAuthConnection
from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig
from orchestrator.tools.jira_oauth_models import JiraOAuthAuthRequiredError, JiraOAuthHttpError

T = TypeVar("T")


def _normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    force_refresh: bool = False,
) -> str:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    expires_at = _normalize_utc_datetime(connection.access_token_expires_at)
    if not force_refresh and expires_at - now > timedelta(seconds=60):
        return decrypt_value(
            ciphertext=connection.access_token_encrypted,
            encryption_key=settings.secrets_encryption_key,
        )

    client = jira_oauth_client(session=session, settings=settings, tenant_id=tenant_id)
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
    connection.access_token_expires_at = _normalize_utc_datetime(token_set.expires_at)
    connection.scopes = token_set.scopes
    connection.updated_at = now
    session.commit()
    return token_set.access_token


def execute_jira_operation_with_refresh_retry(
    *,
    session_factory: Callable[[], Session],
    settings,
    connection_id: str,
    operation: Callable[[Session, JiraOAuthClient, str], T],
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> T:  # noqa: ANN001
    def _run_once(*, force_refresh: bool) -> T:
        with session_factory() as session:
            connection = session.get(JiraOAuthConnection, connection_id)
            if connection is None:
                raise ValueError("Jira OAuth connection record not found.")
            client = jira_oauth_client(
                session=session,
                settings=settings,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            access_token = refresh_jira_connection_tokens(
                session,
                connection=connection,
                settings=settings,
                tenant_id=tenant_id,
                force_refresh=force_refresh,
            )
            return operation(session, client, access_token)

    try:
        return _run_once(force_refresh=False)
    except JiraOAuthHttpError as exc:
        if exc.status_code not in {401, 403}:
            raise
        try:
            return _run_once(force_refresh=True)
        except JiraOAuthHttpError as retry_exc:
            if retry_exc.status_code in {401, 403}:
                raise JiraOAuthAuthRequiredError("Jira OAuth authorization is required") from retry_exc
            raise
