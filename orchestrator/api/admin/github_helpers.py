from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.schemas import GitHubInstallStart, GitHubRepositoryRead
from orchestrator.core.platform_secret_service import PLATFORM_SECRET_GITHUB_APP_SLUG_REF
from orchestrator.core.github_install_state import create_install_state_token, parse_install_state_token
from orchestrator.storage.models import Tenant
from orchestrator.tools.github_app import GitHubApiError


def start_github_install(
    *,
    tenant: Tenant | None,
    tenant_id: str,
    session: Session,
    return_to: str,
    settings,
    resolve_platform_secret_ref_fn,
) -> GitHubInstallStart:  # noqa: ANN001
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    app_slug = resolve_platform_secret_ref_fn(
        session,
        secret_ref=PLATFORM_SECRET_GITHUB_APP_SLUG_REF,
        encryption_key=settings.secrets_encryption_key,
    )
    if app_slug is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub app slug is not configured",
        )
    app_slug = app_slug.strip()
    if not app_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub app slug is not configured",
        )

    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)
    state_token = create_install_state_token(
        tenant_id=tenant_id,
        exp=expires_at,
        secret=settings.github_install_state_secret,
        return_to=return_to,
    )
    install_url = (
        f"https://github.com/apps/{quote(app_slug, safe='')}/installations/new?"
        f"state={quote(state_token, safe='')}"
    )
    return GitHubInstallStart(install_url=install_url, expires_at=expires_at)


def github_install_callback(
    *,
    state_token: str,
    installation_id: str,
    setup_action: str | None,
    session,
    settings,
) -> str:  # noqa: ANN001
    try:
        state = parse_install_state_token(
            token=state_token,
            secret=settings.github_install_state_secret,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    tenant = session.get(Tenant, state.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github_config = dict(tenant.github_config)
    github_config["installation_id"] = str(installation_id)
    if setup_action:
        github_config["installation_setup_action"] = setup_action
    github_config["installation_updated_at"] = datetime.now(timezone.utc).isoformat()
    tenant.github_config = github_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    if state.return_to == "wizard":
        return (
            f"{settings.admin_ui_base_url.rstrip('/')}/tenants/new"
            f"?tenant_id={quote(tenant.tenant_id, safe='')}&github_install=success"
        )
    return (
        f"{settings.admin_ui_base_url.rstrip('/')}/tenants/{quote(tenant.tenant_id, safe='')}/settings/github"
        "?github_install=success"
    )


def list_tenant_github_repositories(
    *,
    tenant: Tenant | None,
    tenant_id: str,
    session,
    settings,
    with_managed_github_refs_fn,
    resolve_scoped_secret_ref_fn,
    resolve_platform_secret_ref_fn,
    github_client_from_tenant_config_fn,
) -> list[GitHubRepositoryRead]:  # noqa: ANN001
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")

    github = tenant.github_config
    if not github.get("installation_id"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub App installation is not connected for this tenant",
        )

    try:
        client = github_client_from_tenant_config_fn(
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
        repositories = client.list_installation_repositories()
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except GitHubApiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return [
        GitHubRepositoryRead(
            full_name=repo.full_name,
            html_url=repo.html_url,
            default_branch=repo.default_branch,
            private=repo.private,
        )
        for repo in repositories
    ]
