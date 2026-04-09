from __future__ import annotations

from sqlalchemy import select


def list_tenants(*, session, tenant_model, tenant_to_schema_fn):  # noqa: ANN001
    tenants = session.execute(select(tenant_model).order_by(tenant_model.tenant_id)).scalars().all()
    return [tenant_to_schema_fn(tenant) for tenant in tenants]


def create_tenant(
    *,
    session,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    create_tenant_fn,
    allocate_tenant_id_fn,
    with_preserved_jira_system_fields_fn,
    with_managed_github_refs_fn,
    with_preserved_discord_system_fields_fn,
    reconcile_tenant_projects_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return create_tenant_fn(
        session=session,
        payload=payload,
        allocate_tenant_id_fn=allocate_tenant_id_fn,
        with_preserved_jira_system_fields_fn=with_preserved_jira_system_fields_fn,
        with_managed_github_refs_fn=with_managed_github_refs_fn,
        with_preserved_discord_system_fields_fn=with_preserved_discord_system_fields_fn,
        reconcile_tenant_projects_fn=reconcile_tenant_projects_fn,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def get_tenant(*, session, tenant_id: str, get_tenant_or_404_fn, tenant_to_schema_fn):  # noqa: ANN001
    return tenant_to_schema_fn(get_tenant_or_404_fn(session=session, tenant_id=tenant_id))


def update_tenant(
    *,
    session,
    tenant_id: str,
    payload,
    validate_codex_assets_for_tenant_init_fn,
    update_tenant_fn,
    with_preserved_jira_system_fields_fn,
    with_managed_github_refs_fn,
    with_preserved_discord_system_fields_fn,
    reconcile_tenant_projects_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    validate_codex_assets_for_tenant_init_fn()
    return update_tenant_fn(
        session=session,
        tenant_id=tenant_id,
        payload=payload,
        with_preserved_jira_system_fields_fn=with_preserved_jira_system_fields_fn,
        with_managed_github_refs_fn=with_managed_github_refs_fn,
        with_preserved_discord_system_fields_fn=with_preserved_discord_system_fields_fn,
        reconcile_tenant_projects_fn=reconcile_tenant_projects_fn,
        tenant_to_schema_fn=tenant_to_schema_fn,
    )


def delete_tenant(*, session, tenant_id: str, delete_tenant_fn):  # noqa: ANN001
    return delete_tenant_fn(session=session, tenant_id=tenant_id)


def set_tenant_archive_state(
    *,
    session,
    tenant_id: str,
    is_enabled: bool,
    set_tenant_archive_state_fn,
    tenant_to_schema_fn,
    archive_retention_days: int,
):  # noqa: ANN001
    return set_tenant_archive_state_fn(
        session=session,
        tenant_id=tenant_id,
        is_enabled=is_enabled,
        tenant_to_schema_fn=tenant_to_schema_fn,
        archive_retention_days=archive_retention_days,
    )


def list_projects(*, session, tenant_id: str, admin_project_service_factory):  # noqa: ANN001
    service = admin_project_service_factory()
    return service.list_projects(session=session, tenant_id=tenant_id)


def create_project(*, session, tenant_id: str, payload, admin_project_service_factory):  # noqa: ANN001
    service = admin_project_service_factory()
    return service.create_project(session=session, tenant_id=tenant_id, payload=payload)


def get_project(*, session, tenant_id: str, project_id: str, admin_project_service_factory):  # noqa: ANN001
    service = admin_project_service_factory()
    return service.get_project(session=session, tenant_id=tenant_id, project_id=project_id)


def update_project(*, session, tenant_id: str, project_id: str, payload, admin_project_service_factory):  # noqa: ANN001
    service = admin_project_service_factory()
    return service.update_project(session=session, tenant_id=tenant_id, project_id=project_id, payload=payload)
