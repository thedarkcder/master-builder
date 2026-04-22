from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.schemas import IntegrationTestResult
from orchestrator.core.decision_types import (
    JiraConfigKey,
)
from orchestrator.core.tenant_operational_health_service import tenant_integration_snapshot
from orchestrator.storage.models import AtlassianOAuthConnection, Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError


def test_atlassian_connection(
    *,
    session,
    tenant_id: str,
    settings,
    refresh_atlassian_connection_tokens_fn,
    atlassian_oauth_client_fn,
) -> IntegrationTestResult:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    integration = tenant_integration_snapshot(tenant=tenant)
    missing: list[str] = []
    if not integration.jira_project_keys:
        missing.append(JiraConfigKey.PROJECT_KEYS.value)
    if missing:
        return IntegrationTestResult(ok=False, details=f"Missing Jira fields: {', '.join(missing)}")

    connection_id = integration.atlassian_connection_id
    if not connection_id:
        return IntegrationTestResult(ok=False, details="Atlassian connection is not linked for this tenant")

    connection = session.get(AtlassianOAuthConnection, connection_id)
    if connection is None:
        return IntegrationTestResult(ok=False, details="Configured Atlassian connection was not found")

    try:
        access_token = refresh_atlassian_connection_tokens_fn(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant_id,
        )
        client = atlassian_oauth_client_fn(session=session, settings=settings, tenant_id=tenant_id)
        projects = client.list_projects(access_token=access_token, cloud_id=connection.cloud_id)
    except (ValueError, AtlassianOAuthError) as exc:
        return IntegrationTestResult(ok=False, details=f"Atlassian validation failed: {exc}")

    return IntegrationTestResult(
        ok=True,
        details=(
            f"Atlassian connection is valid for {connection.site_url}; "
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
