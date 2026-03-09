from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.orm import Session

from orchestrator.api.admin.config_helpers import (
    validate_codex_assets_for_tenant_init as validate_codex_assets_for_tenant_init_core,
)
from orchestrator.api.admin.route_helpers import (
    admin_project_service,
    allocate_tenant_id,
    ensure_default_project_for_tenant,
    sync_tenant_jira_project_keys,
    with_managed_github_refs,
    with_preserved_jira_system_fields,
)
from orchestrator.api.admin.schema_mappers import tenant_to_schema
from orchestrator.api.admin.project_normalization import (
    with_preserved_discord_system_fields,
)
from orchestrator.api.admin.tenant_crud import (
    create_tenant as create_tenant_impl,
    delete_tenant as delete_tenant_impl,
    get_tenant_or_404 as get_tenant_or_404_impl,
    set_tenant_archive_state as set_tenant_archive_state_impl,
    update_tenant as update_tenant_impl,
)
from orchestrator.api.admin.tenant_project_routes_service import (
    create_project as create_project_route_impl,
    create_tenant as create_tenant_route_impl,
    delete_tenant as delete_tenant_route_impl,
    get_project as get_project_route_impl,
    get_tenant as get_tenant_route_impl,
    list_projects as list_projects_route_impl,
    list_tenants as list_tenants_route_impl,
    set_tenant_archive_state as set_tenant_archive_state_route_impl,
    update_project as update_project_route_impl,
    update_tenant as update_tenant_route_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    ProjectCreate,
    ProjectRead,
    ProjectUpdate,
    TenantCreate,
    TenantRead,
    TenantUpdate,
)
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin
from orchestrator.storage.models import Tenant

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _validate_codex_assets_for_tenant_init() -> None:
    validate_codex_assets_for_tenant_init_core(
        settings=get_settings(),
        module_file=__file__,
    )


@router.get("/tenants", response_model=list[TenantRead])
def list_tenants(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[TenantRead]:
    return list_tenants_route_impl(
        session=session,
        tenant_model=Tenant,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.post("/tenants", response_model=TenantRead, status_code=status.HTTP_201_CREATED)
def create_tenant(
    payload: TenantCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return create_tenant_route_impl(
        session=session,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        create_tenant_fn=create_tenant_impl,
        allocate_tenant_id_fn=allocate_tenant_id,
        with_preserved_jira_system_fields_fn=with_preserved_jira_system_fields,
        with_managed_github_refs_fn=with_managed_github_refs,
        with_preserved_discord_system_fields_fn=with_preserved_discord_system_fields,
        ensure_default_project_for_tenant_fn=ensure_default_project_for_tenant,
        sync_tenant_jira_project_keys_fn=sync_tenant_jira_project_keys,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return get_tenant_route_impl(
        session=session,
        tenant_id=tenant_id,
        get_tenant_or_404_fn=get_tenant_or_404_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.put("/tenants/{tenant_id}", response_model=TenantRead)
def update_tenant(
    tenant_id: str,
    payload: TenantUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return update_tenant_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        validate_codex_assets_for_tenant_init_fn=_validate_codex_assets_for_tenant_init,
        update_tenant_fn=update_tenant_impl,
        with_preserved_jira_system_fields_fn=with_preserved_jira_system_fields,
        with_managed_github_refs_fn=with_managed_github_refs,
        with_preserved_discord_system_fields_fn=with_preserved_discord_system_fields,
        ensure_default_project_for_tenant_fn=ensure_default_project_for_tenant,
        sync_tenant_jira_project_keys_fn=sync_tenant_jira_project_keys,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.delete("/tenants/{tenant_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    return delete_tenant_route_impl(
        session=session,
        tenant_id=tenant_id,
        delete_tenant_fn=delete_tenant_impl,
    )


@router.post("/tenants/{tenant_id}/archive", response_model=TenantRead)
def archive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return set_tenant_archive_state_route_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=False,
        set_tenant_archive_state_fn=set_tenant_archive_state_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.post("/tenants/{tenant_id}/unarchive", response_model=TenantRead)
def unarchive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    return set_tenant_archive_state_route_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=True,
        set_tenant_archive_state_fn=set_tenant_archive_state_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.get("/tenants/{tenant_id}/projects", response_model=list[ProjectRead])
def list_projects(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ProjectRead]:
    return list_projects_route_impl(
        session=session,
        tenant_id=tenant_id,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.post("/tenants/{tenant_id}/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    tenant_id: str,
    payload: ProjectCreate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    return create_project_route_impl(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.get("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectRead)
def get_project(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    return get_project_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.put("/tenants/{tenant_id}/projects/{project_id}", response_model=ProjectRead)
def update_project(
    tenant_id: str,
    project_id: str,
    payload: ProjectUpdate,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectRead:
    return update_project_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]
