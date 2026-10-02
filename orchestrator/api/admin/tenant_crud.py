from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Response, status
from sqlalchemy import delete, select

from orchestrator.storage.models import (
    ManagedSecret,
    Project,
    Run,
    Tenant,
    TenantInvite,
    TenantMembership,
    TenantRunClaim,
    TenantTeam,
    TenantTeamMembership,
    TenantUser,
    TenantUserCredential,
    TenantUserDiscordIdentity,
)
from orchestrator.api.admin.deployment_config_service import (
    normalize_tenant_deployment_plane,
    tenant_deployment_plane_to_schema,
)


def create_tenant(
    *,
    session,
    payload,
    allocate_tenant_id_fn,
    with_preserved_jira_system_fields_fn,
    with_managed_github_refs_fn,
    with_preserved_discord_system_fields_fn,
    reconcile_tenant_projects_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    tenant_id = allocate_tenant_id_fn(session, name=payload.name)
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        tenant_id=tenant_id,
        name=payload.name,
        is_enabled=payload.is_enabled,
        archived_at=None,
        purge_after_at=None,
        jira_config=with_preserved_jira_system_fields_fn(
            existing={},
            proposed=payload.jira.model_dump(),
        ),
        github_config=with_managed_github_refs_fn(payload.github.model_dump()),
        repos_config=payload.repos.model_dump(),
        policy_config=payload.policy.model_dump(),
        discord_config=with_preserved_discord_system_fields_fn(
            existing={},
            proposed=payload.discord.model_dump(exclude_unset=True)
            if payload.discord
            else None,
        ),
        experience_config=dict(payload.experience),
        setup_state=dict(payload.setup_state),
        deployment_plane_config={},
        created_at=now,
        updated_at=now,
    )
    session.add(tenant)
    session.add(
        TenantRunClaim(
            tenant_id=tenant_id,
            updated_at=now,
        )
    )
    reconcile_tenant_projects_fn(session, tenant=tenant)
    session.commit()
    session.refresh(tenant)
    return tenant_to_schema_fn(tenant)


def get_tenant_or_404(*, session, tenant_id: str) -> Tenant:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    return tenant


def _commit_tenant_update(*, session, tenant, tenant_to_schema_fn):  # noqa: ANN001
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)
    return tenant_to_schema_fn(tenant)


def update_tenant_configuration(
    *, session, tenant_id: str, payload, tenant_to_schema_fn
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.name = payload.name
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def update_tenant_jira(
    *,
    session,
    tenant_id: str,
    payload,
    with_preserved_jira_system_fields_fn,
    reconcile_tenant_projects_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.jira_config = with_preserved_jira_system_fields_fn(
        existing=dict(tenant.jira_config or {}),
        proposed=payload.jira.model_dump(exclude_unset=True),
    )
    reconcile_tenant_projects_fn(session, tenant=tenant)
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def update_tenant_github(
    *,
    session,
    tenant_id: str,
    payload,
    with_managed_github_refs_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.github_config = with_managed_github_refs_fn(payload.github.model_dump())
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def update_tenant_repos(
    *,
    session,
    tenant_id: str,
    payload,
    reconcile_tenant_projects_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.repos_config = payload.repos.model_dump()
    reconcile_tenant_projects_fn(session, tenant=tenant)
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def update_tenant_policy(*, session, tenant_id: str, payload, tenant_to_schema_fn):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.policy_config = payload.policy.model_dump()
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def update_tenant_observability(
    *, session, tenant_id: str, payload, tenant_to_schema_fn
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    policy_config = dict(tenant.policy_config or {})
    policy_config["observability"] = payload.observability.model_dump()
    tenant.policy_config = policy_config
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def update_tenant_discord(
    *,
    session,
    tenant_id: str,
    payload,
    with_preserved_discord_system_fields_fn,
    tenant_to_schema_fn,
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.discord_config = with_preserved_discord_system_fields_fn(
        existing=dict(tenant.discord_config or {}),
        proposed=payload.discord.model_dump(exclude_unset=True)
        if payload.discord
        else None,
    )
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def update_tenant_experience(*, session, tenant_id: str, payload, tenant_to_schema_fn):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.experience_config = dict(payload.experience)
    tenant.setup_state = dict(payload.setup_state)
    return _commit_tenant_update(
        session=session, tenant=tenant, tenant_to_schema_fn=tenant_to_schema_fn
    )


def _delete_tenant_owned_secrets(*, session, tenant_id: str) -> None:  # noqa: ANN001
    session.execute(
        delete(ManagedSecret).where(
            (ManagedSecret.secret_ref.like(f"tenant/{tenant_id}/%"))
            | (ManagedSecret.secret_ref.like(f"project/{tenant_id}/%"))
        )
    )


def _delete_orphan_tenant_users(*, session, candidate_user_ids: list[str]) -> None:  # noqa: ANN001
    if not candidate_user_ids:
        return
    for user_id in sorted(set(candidate_user_ids)):
        has_remaining_membership = session.execute(
            select(TenantMembership.membership_id)
            .where(TenantMembership.user_id == user_id)
            .limit(1)
        ).scalar_one_or_none()
        if has_remaining_membership is None:
            credential = session.get(TenantUserCredential, user_id)
            if credential is not None:
                session.delete(credential)
            discord_identity = session.get(TenantUserDiscordIdentity, user_id)
            if discord_identity is not None:
                session.delete(discord_identity)
            tenant_user = session.get(TenantUser, user_id)
            if tenant_user is not None:
                session.delete(tenant_user)


def _delete_tenant_and_owned_data(*, session, tenant_id: str) -> None:  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    affected_user_ids = list(
        session.execute(
            select(TenantMembership.user_id).where(
                TenantMembership.tenant_id == tenant_id
            )
        ).scalars()
    )
    membership_ids = list(
        session.execute(
            select(TenantMembership.membership_id).where(
                TenantMembership.tenant_id == tenant_id
            )
        ).scalars()
    )
    team_ids = list(
        session.execute(
            select(TenantTeam.team_id).where(TenantTeam.tenant_id == tenant_id)
        ).scalars()
    )
    session.execute(delete(Run).where(Run.tenant_id == tenant_id))
    session.execute(delete(TenantRunClaim).where(TenantRunClaim.tenant_id == tenant_id))
    session.execute(delete(TenantInvite).where(TenantInvite.tenant_id == tenant_id))
    if membership_ids:
        session.execute(
            delete(TenantTeamMembership).where(
                TenantTeamMembership.membership_id.in_(membership_ids)
            )
        )
    if team_ids:
        session.execute(
            delete(TenantTeamMembership).where(
                TenantTeamMembership.team_id.in_(team_ids)
            )
        )
    session.execute(delete(TenantTeam).where(TenantTeam.tenant_id == tenant_id))
    session.execute(delete(Project).where(Project.tenant_id == tenant_id))
    session.execute(
        delete(TenantMembership).where(TenantMembership.tenant_id == tenant_id)
    )
    _delete_tenant_owned_secrets(session=session, tenant_id=tenant_id)
    session.delete(tenant)
    session.flush()
    _delete_orphan_tenant_users(session=session, candidate_user_ids=affected_user_ids)


def delete_tenant(*, session, tenant_id: str) -> Response:  # noqa: ANN001
    _delete_tenant_and_owned_data(session=session, tenant_id=tenant_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def set_tenant_archive_state(
    *,
    session,
    tenant_id: str,
    is_enabled: bool,
    tenant_to_schema_fn,
    archive_retention_days: int,
):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.is_enabled = is_enabled
    now = datetime.now(timezone.utc)
    if is_enabled:
        tenant.archived_at = None
        tenant.purge_after_at = None
    else:
        tenant.archived_at = now
        tenant.purge_after_at = now + timedelta(
            days=max(1, int(archive_retention_days))
        )
    tenant.updated_at = now
    session.commit()
    session.refresh(tenant)
    return tenant_to_schema_fn(tenant)


def purge_expired_archived_tenants(*, session, now: datetime | None = None) -> int:  # noqa: ANN001
    effective_now = now or datetime.now(timezone.utc)
    expired_tenant_ids = list(
        session.execute(
            select(Tenant.tenant_id).where(
                Tenant.is_enabled.is_(False),
                Tenant.purge_after_at.is_not(None),
                Tenant.purge_after_at <= effective_now,
            )
        ).scalars()
    )
    if not expired_tenant_ids:
        return 0
    for tenant_id in expired_tenant_ids:
        _delete_tenant_and_owned_data(session=session, tenant_id=tenant_id)
    session.commit()
    return len(expired_tenant_ids)


def get_tenant_deployment_plane(*, session, tenant_id: str):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    return tenant_deployment_plane_to_schema(tenant)


def update_tenant_deployment_plane(*, session, tenant_id: str, payload):  # noqa: ANN001
    tenant = get_tenant_or_404(session=session, tenant_id=tenant_id)
    tenant.deployment_plane_config = normalize_tenant_deployment_plane(payload)
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(tenant)
    return tenant_deployment_plane_to_schema(tenant)
