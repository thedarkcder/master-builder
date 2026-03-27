from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException, Response, status
from sqlalchemy import delete

from orchestrator.storage.models import Run, Tenant


def create_tenant(
    *,
    session,
    payload,
    allocate_tenant_id_fn,
    with_preserved_jira_system_fields_fn,
    with_managed_github_refs_fn,
    with_preserved_discord_system_fields_fn,
    ensure_default_project_for_tenant_fn,
    sync_tenant_jira_project_keys_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    tenant_id = allocate_tenant_id_fn(session, name=payload.name)
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        tenant_id=tenant_id,
        name=payload.name,
        is_enabled=payload.is_enabled,
        jira_config=with_preserved_jira_system_fields_fn(
            existing={},
            proposed=payload.jira.model_dump(),
        ),
        github_config=with_managed_github_refs_fn(payload.github.model_dump()),
        repos_config=payload.repos.model_dump(),
        policy_config=payload.policy.model_dump(),
        discord_config=with_preserved_discord_system_fields_fn(
            existing={},
            proposed=payload.discord.model_dump(exclude_unset=True) if payload.discord else None,
        ),
        experience_config=dict(payload.experience),
        setup_state=dict(payload.setup_state),
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
    ensure_default_project_for_tenant_fn(session, tenant=tenant)
    sync_tenant_jira_project_keys_fn(session, tenant=tenant)
    session.commit()
    session.refresh(tenant)
    return tenant_to_schema_fn(tenant)


def get_tenant_or_404(*, session, tenant_id: str) -> Tenant:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    return tenant


def update_tenant(
    *,
    session,
    tenant_id: str,
    payload,
    with_preserved_jira_system_fields_fn,
    with_managed_github_refs_fn,
    with_preserved_discord_system_fields_fn,
    ensure_default_project_for_tenant_fn,
    sync_tenant_jira_project_keys_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)

    tenant.name = payload.name
    tenant.is_enabled = payload.is_enabled
    tenant.jira_config = with_preserved_jira_system_fields_fn(
        existing=dict(tenant.jira_config),
        proposed=payload.jira.model_dump(exclude_unset=True),
    )
    tenant.github_config = with_managed_github_refs_fn(payload.github.model_dump())
    tenant.repos_config = payload.repos.model_dump()
    tenant.policy_config = payload.policy.model_dump()
    tenant.discord_config = with_preserved_discord_system_fields_fn(
        existing=dict(tenant.discord_config or {}),
        proposed=payload.discord.model_dump(exclude_unset=True) if payload.discord else None,
    )
    tenant.experience_config = dict(payload.experience)
    tenant.setup_state = dict(payload.setup_state)
    tenant.updated_at = datetime.now(timezone.utc)
    ensure_default_project_for_tenant_fn(session, tenant=tenant)
    sync_tenant_jira_project_keys_fn(session, tenant=tenant)

    session.commit()
    session.refresh(tenant)
    return tenant_to_schema_fn(tenant)


def delete_tenant(*, session, tenant_id: str) -> Response:  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    session.execute(delete(Run).where(Run.tenant_id == tenant_id))
    session.delete(tenant)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def set_tenant_archive_state(*, session, tenant_id: str, is_enabled: bool, tenant_to_schema_fn):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.is_enabled = is_enabled
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)
    return tenant_to_schema_fn(tenant)
