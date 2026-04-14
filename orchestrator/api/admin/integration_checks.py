from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.schemas import IntegrationTestResult
from orchestrator.core.decision_types import (
    JiraConfigKey,
    jira_config_project_keys,
    tenant_jira_config_text,
)
from orchestrator.storage.models import JiraOAuthConnection, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError


def test_jira_connection(
    *,
    session,
    tenant_id: str,
    settings,
    refresh_jira_connection_tokens_fn,
    jira_oauth_client_fn,
) -> IntegrationTestResult:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    jira = tenant.jira_config
    missing: list[str] = []
    if not jira_config_project_keys(jira_config=jira):
        missing.append(JiraConfigKey.PROJECT_KEYS.value)
    if missing:
        return IntegrationTestResult(ok=False, details=f"Missing Jira fields: {', '.join(missing)}")

    connection_id = tenant_jira_config_text(tenant=tenant, key=JiraConfigKey.CONNECTION_ID)
    if not connection_id:
        return IntegrationTestResult(ok=False, details="Jira OAuth connection is not linked for this tenant")

    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        return IntegrationTestResult(ok=False, details="Configured Jira connection was not found")

    try:
        access_token = refresh_jira_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant_id,
        )
        client = jira_oauth_client_fn(session=session, settings=settings, tenant_id=tenant_id)
        projects = client.list_projects(access_token=access_token, cloud_id=connection.cloud_id)
    except (ValueError, JiraOAuthError) as exc:
        return IntegrationTestResult(ok=False, details=f"Jira OAuth validation failed: {exc}")

    return IntegrationTestResult(
        ok=True,
        details=(
            f"Jira OAuth connection is valid for {connection.site_url}; "
            f"{len(projects)} project(s) visible"
        ),
    )


def test_github_connection(
    *,
    session,
    tenant_id: str,
    settings,
    with_managed_github_refs_fn,
    resolve_scoped_secret_ref_fn,
    resolve_platform_secret_ref_fn,
    github_client_from_tenant_config_fn,
) -> IntegrationTestResult:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github = tenant.github_config
    if not github.get("installation_id"):
        return IntegrationTestResult(
            ok=False,
            details="GitHub App installation is not connected for this tenant",
        )

    try:
        github_client_from_tenant_config_fn(
            with_managed_github_refs_fn(github),
            tenant_secret_lookup=lambda ref: resolve_scoped_secret_ref_fn(
                session,
                secret_ref=ref,
                encryption_key=settings.secrets_encryption_key,
                tenant_id=tenant_id,
            ),
            platform_secret_lookup=lambda ref: resolve_platform_secret_ref_fn(
                session,
                secret_ref=ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
    except ValueError as exc:
        return IntegrationTestResult(ok=False, details=str(exc))

    return IntegrationTestResult(
        ok=True,
        details="GitHub tenant configuration looks valid and secret refs resolve",
    )
