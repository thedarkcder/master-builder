from __future__ import annotations

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.github_helpers import (
    github_install_callback as github_install_callback_impl,
    list_project_github_branches as list_project_github_branches_impl,
    list_tenant_github_repositories as list_tenant_github_repositories_impl,
    start_github_install as start_github_install_impl,
)
from orchestrator.api.admin.integration_checks import (
    test_github_connection as test_github_connection_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    GitHubBranchRead,
    GitHubInstallStart,
    GitHubRepositoryRead,
    IntegrationTestResult,
)
from orchestrator.core.config import get_settings
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_admin,
    require_any_tenant_permission,
    require_authenticated_principal,
    require_tenant_permission,
)
from orchestrator.core.platform.access import (
    PERMISSION_PROJECTS_MANAGE,
    PERMISSION_WORKSPACE_MANAGE,
)
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import Project, Tenant

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.post(
    "/tenants/{tenant_id}/github/install/start", response_model=GitHubInstallStart
)
def start_github_install(
    tenant_id: str,
    return_to: str = Query(default="edit", pattern="^(edit|wizard)$"),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> GitHubInstallStart:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    else:
        require_tenant_permission(
            principal=principal,
            tenant_id=tenant_id,
            permission_key=PERMISSION_WORKSPACE_MANAGE,
        )
    return start_github_install_impl(
        tenant=session.get(Tenant, tenant_id),
        tenant_id=tenant_id,
        return_to=return_to,
        session=session,
        settings=get_settings(),
        resolve_platform_secret_ref_fn=lambda db_session, secret_ref, encryption_key: (
            resolve_platform_secret_ref(
                db_session,
                secret_ref=secret_ref,
                encryption_key=encryption_key,
            )
        ),
    )


@router.get("/github/install/callback", include_in_schema=False)
def github_install_callback(
    state_token: str = Query(..., alias="state"),
    installation_id: str = Query(..., min_length=1),
    setup_action: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    redirect_url = github_install_callback_impl(
        state_token=state_token,
        installation_id=installation_id,
        setup_action=setup_action,
        session=session,
        settings=get_settings(),
    )
    return RedirectResponse(url=redirect_url, status_code=status.HTTP_302_FOUND)


@router.get(
    "/tenants/{tenant_id}/github/repositories",
    response_model=list[GitHubRepositoryRead],
)
def list_tenant_github_repositories(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[GitHubRepositoryRead]:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    else:
        require_any_tenant_permission(
            principal=principal,
            tenant_id=tenant_id,
            permission_keys=(PERMISSION_WORKSPACE_MANAGE, PERMISSION_PROJECTS_MANAGE),
        )
    return list_tenant_github_repositories_impl(
        tenant=session.get(Tenant, tenant_id),
        tenant_id=tenant_id,
        session=session,
        settings=get_settings(),
        with_managed_github_refs_fn=deps.with_managed_github_refs,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        github_client_from_tenant_config_fn=deps.github_client_from_tenant_config,
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/github/branches",
    response_model=list[GitHubBranchRead],
)
def list_project_github_branches(
    tenant_id: str,
    project_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[GitHubBranchRead]:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    else:
        require_any_tenant_permission(
            principal=principal,
            tenant_id=tenant_id,
            permission_keys=(PERMISSION_WORKSPACE_MANAGE, PERMISSION_PROJECTS_MANAGE),
        )
    return list_project_github_branches_impl(
        tenant=session.get(Tenant, tenant_id),
        project=session.get(Project, project_id),
        tenant_id=tenant_id,
        session=session,
        settings=get_settings(),
        with_managed_github_refs_fn=deps.with_managed_github_refs,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        github_client_from_tenant_config_fn=deps.github_client_from_tenant_config,
    )


@router.post("/tenants/{tenant_id}/test-github", response_model=IntegrationTestResult)
def test_github_connection(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> IntegrationTestResult:
    if principal.is_platform_super_admin:
        require_admin(principal=principal)
    else:
        require_tenant_permission(
            principal=principal,
            tenant_id=tenant_id,
            permission_key=PERMISSION_WORKSPACE_MANAGE,
        )
    return test_github_connection_impl(
        session=session,
        tenant_id=tenant_id,
        settings=get_settings(),
        with_managed_github_refs_fn=deps.with_managed_github_refs,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        github_client_from_tenant_config_fn=deps.github_client_from_tenant_config,
    )
