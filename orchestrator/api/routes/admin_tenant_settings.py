from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from orchestrator.api.admin.config_helpers import (
    validate_codex_assets_for_tenant_init as validate_codex_assets_for_tenant_init_core,
)
from orchestrator.api.admin.project_normalization import (
    with_preserved_discord_system_fields,
)
from orchestrator.api.admin.route_helpers import (
    reconcile_tenant_projects,
    with_managed_github_refs,
    with_preserved_jira_system_fields,
)
from orchestrator.api.admin.schema_mappers import tenant_to_schema
from orchestrator.api.admin.tenant_crud import (
    update_tenant_configuration as update_tenant_configuration_impl,
    update_tenant_discord as update_tenant_discord_impl,
    update_tenant_experience as update_tenant_experience_impl,
    update_tenant_github as update_tenant_github_impl,
    update_tenant_jira as update_tenant_jira_impl,
    update_tenant_observability as update_tenant_observability_impl,
    update_tenant_policy as update_tenant_policy_impl,
    update_tenant_repos as update_tenant_repos_impl,
)
from orchestrator.api.admin.tenant_settings_route_service import (
    update_tenant_configuration as update_tenant_configuration_route_impl,
    update_tenant_discord as update_tenant_discord_route_impl,
    update_tenant_experience as update_tenant_experience_route_impl,
    update_tenant_github as update_tenant_github_route_impl,
    update_tenant_jira as update_tenant_jira_route_impl,
    update_tenant_observability as update_tenant_observability_route_impl,
    update_tenant_policy as update_tenant_policy_route_impl,
    update_tenant_repos as update_tenant_repos_route_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    TenantConfigurationUpdate,
    TenantDiscordUpdate,
    TenantExperienceUpdate,
    TenantGithubUpdate,
    TenantJiraUpdate,
    TenantObservabilityUpdate,
    TenantPolicyUpdate,
    TenantRead,
    TenantReposUpdate,
)
from orchestrator.core.config import get_settings
from orchestrator.core.platform.access import PERMISSION_WORKSPACE_MANAGE
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_authenticated_principal,
    require_tenant_permission,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _validate_codex_assets_for_tenant_init() -> None:
    validate_codex_assets_for_tenant_init_core(
        settings=get_settings(),
        module_file=__file__,
    )


@router.patch("/tenants/{tenant_id}/configuration", response_model=TenantRead)
def update_tenant_configuration(
    tenant_id: str,
    payload: TenantConfigurationUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_configuration_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_configuration_fn=update_tenant_configuration_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.patch("/tenants/{tenant_id}/jira", response_model=TenantRead)
def update_tenant_jira(
    tenant_id: str,
    payload: TenantJiraUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_jira_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_jira_fn=update_tenant_jira_impl,
        with_preserved_jira_system_fields_fn=with_preserved_jira_system_fields,
        reconcile_tenant_projects_fn=reconcile_tenant_projects,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.patch("/tenants/{tenant_id}/github", response_model=TenantRead)
def update_tenant_github(
    tenant_id: str,
    payload: TenantGithubUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_github_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_github_fn=update_tenant_github_impl,
        with_managed_github_refs_fn=with_managed_github_refs,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.patch("/tenants/{tenant_id}/repos", response_model=TenantRead)
def update_tenant_repos(
    tenant_id: str,
    payload: TenantReposUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_repos_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_repos_fn=update_tenant_repos_impl,
        reconcile_tenant_projects_fn=reconcile_tenant_projects,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.patch("/tenants/{tenant_id}/policy", response_model=TenantRead)
def update_tenant_policy(
    tenant_id: str,
    payload: TenantPolicyUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_policy_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_policy_fn=update_tenant_policy_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.patch("/tenants/{tenant_id}/observability", response_model=TenantRead)
def update_tenant_observability(
    tenant_id: str,
    payload: TenantObservabilityUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_observability_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_observability_fn=update_tenant_observability_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.patch("/tenants/{tenant_id}/discord", response_model=TenantRead)
def update_tenant_discord(
    tenant_id: str,
    payload: TenantDiscordUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_discord_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_discord_fn=update_tenant_discord_impl,
        with_preserved_discord_system_fields_fn=with_preserved_discord_system_fields,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.patch("/tenants/{tenant_id}/experience", response_model=TenantRead)
def update_tenant_experience(
    tenant_id: str,
    payload: TenantExperienceUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_WORKSPACE_MANAGE,
    )
    return update_tenant_experience_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_experience_fn=update_tenant_experience_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )
