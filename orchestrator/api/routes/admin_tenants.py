from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.orm import Session

from orchestrator.api.admin.config_helpers import (
    validate_codex_assets_for_tenant_init as validate_codex_assets_for_tenant_init_core,
)
from orchestrator.api.admin.route_helpers import (
    admin_project_service,
    allocate_tenant_id,
    reconcile_tenant_projects,
    with_managed_github_refs,
    with_preserved_jira_system_fields,
)
from orchestrator.api.admin.schema_mappers import (
    project_install_request_to_schema,
    project_install_to_schema,
    tenant_to_schema,
)
from orchestrator.api.admin.project_normalization import (
    with_preserved_discord_system_fields,
)
from orchestrator.api.admin.tenant_crud import (
    create_tenant as create_tenant_impl,
    delete_tenant as delete_tenant_impl,
    get_tenant_or_404 as get_tenant_or_404_impl,
    set_tenant_archive_state as set_tenant_archive_state_impl,
)
from orchestrator.api.admin.tenant_project_routes_service import (
    create_project as create_project_route_impl,
    create_tenant as create_tenant_route_impl,
    delete_tenant as delete_tenant_route_impl,
    get_project as get_project_route_impl,
    get_tenant as get_tenant_route_impl,
    list_projects as list_projects_route_impl,
    list_tenants as list_tenants_route_impl,
    resolve_project_jira_run_board as resolve_project_jira_run_board_route_impl,
    set_tenant_archive_state as set_tenant_archive_state_route_impl,
    update_project_archive_state as update_project_archive_state_route_impl,
    update_project_configuration as update_project_configuration_route_impl,
    update_project_discord as update_project_discord_route_impl,
    update_project_environment as update_project_environment_route_impl,
    update_project_policy as update_project_policy_route_impl,
    update_project_secret_refs as update_project_secret_refs_route_impl,
)
from orchestrator.api.routes.app_auth import invite_to_schema
from orchestrator.api.dependencies import get_session
from orchestrator.api.url_helpers import resolve_public_base_url
from orchestrator.api.schemas import (
    ProjectCreate,
    ProjectAutomationExecutionRead,
    ProjectAutomationRead,
    ProjectAutomationsRead,
    ProjectAutomationsWrite,
    ProjectArchiveUpdate,
    ProjectConfigurationUpdate,
    ProjectDiscordUpdate,
    ProjectEnvironmentUpdate,
    ProjectInstallRead,
    ProjectInstallRequestRead,
    ProjectInstallRequestUpdate,
    ProjectInstallRequestsRead,
    ProjectInstallsRead,
    ProjectInstallWrite,
    ProjectPolicyUpdate,
    ProjectRead,
    ProjectSecretRefsUpdate,
    TenantDeliverySummaryRead,
    TenantDiscordIdentityRead,
    TenantDiscordInviteRead,
    TenantDiscordLinkStartRead,
    TenantInviteCreate,
    TenantInviteActionResult,
    TenantInviteListRead,
    TenantInviteRead,
    TenantMemberRead,
    TenantMemberUpdate,
    TenantTeamCreate,
    TenantTeamRead,
    TenantTeamUpdate,
    TenantCreate,
    TenantRead,
)
from orchestrator.core.platform.install_registry_service import (
    ProjectInstallWrite as ServiceProjectInstallWrite,
    create_project_install,
    delete_project_install,
    get_project_install,
    list_project_installs,
    update_project_install,
)
from orchestrator.core.platform.install_request_service import (
    INSTALL_REQUEST_STATUS_APPROVED,
    INSTALL_REQUEST_STATUS_REJECTED,
    approve_install_request,
    get_project_install_request,
    list_project_install_requests,
    reject_install_request,
    update_install_request_status,
)
from orchestrator.core.config import get_settings
from orchestrator.core.discord.oauth import (
    DiscordOAuthError,
    build_discord_oauth_authorize_url,
    discord_oauth_is_configured,
    issue_discord_oauth_state,
)
from orchestrator.core.discord.oauth_config import resolve_discord_oauth_config
from orchestrator.core.platform.email_delivery import EmailDeliveryError
from orchestrator.core.invites import email_delivery
from orchestrator.core.platform.secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.projects.automation_service import (
    enqueue_project_automation_run_now,
    ProjectAutomationWrite as ServiceProjectAutomationWrite,
    list_execution_history,
    list_project_automation_definitions,
    upsert_project_automation,
)
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    TenantMembershipPrincipal,
    load_tenant_user_principal,
    require_admin,
    require_authenticated_principal,
    require_tenant_membership,
    require_tenant_permission,
)
from orchestrator.core.platform.access import (
    KNOWN_PERMISSION_KEYS,
    PERMISSION_PEOPLE_MANAGE,
    PERMISSION_PROJECTS_MANAGE,
    normalize_permission_keys,
)
from orchestrator.core.platform.users import (
    create_team,
    create_invite,
    ensure_team_ids_exist,
    get_discord_identity,
    get_invite,
    list_teams,
    list_invites,
    list_memberships_with_users,
    resend_invite,
    revoke_invite,
    summarize_delivery,
    update_membership,
    update_membership_discord_state,
    update_team,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _validate_codex_assets_for_tenant_init() -> None:
    validate_codex_assets_for_tenant_init_core(
        settings=get_settings(),
        module_file=__file__,
    )


def _build_invite_url(*, request: Request, raw_token: str) -> str:
    settings = get_settings()
    base_url = resolve_public_base_url(request=request, configured_base_url=settings.admin_ui_base_url)
    return f"{base_url}/invite/accept?token={raw_token}"


def _send_tenant_invite_email_or_raise(
    *,
    email: str,
    full_name: str | None,
    invite_url: str,
    tenant_name: str,
) -> None:
    try:
        email_delivery.send_tenant_invite_email(
            email=email,
            full_name=full_name,
            invite_url=invite_url,
            tenant_name=tenant_name,
        )
    except EmailDeliveryError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invite email delivery is unavailable",
        ) from exc


def _invite_to_schema_with_url(invite: object, *, invite_url: str | None = None) -> TenantInviteRead:
    return invite_to_schema(invite, invite_url=invite_url)


def _team_to_schema(team) -> TenantTeamRead:  # noqa: ANN001
    return TenantTeamRead(
        team_id=team.team_id,
        tenant_id=team.tenant_id,
        name=team.name,
        description=team.description,
        permission_keys=list(team.permission_keys or []),
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


def _member_to_schema(*, principal: TenantMembershipPrincipal, user, created_at: datetime, updated_at: datetime) -> TenantMemberRead:  # noqa: ANN001
    return TenantMemberRead(
        membership_id=principal.membership_id,
        tenant_id=principal.tenant_id,
        user_id=user.user_id,
        email=user.email,
        full_name=user.full_name,
        is_active=bool(user.is_active),
        role=principal.role,
        permission_keys=list(principal.permission_keys),
        effective_mode=principal.effective_mode,
        mode_override=principal.mode_override,
        onboarding_kind=principal.onboarding_kind,
        first_signed_in_at=principal.first_signed_in_at,
        onboarding_completed_at=principal.onboarding_completed_at,
        onboarding_version=principal.onboarding_version,
        team_ids=list(principal.team_ids),
        discord_state=dict(principal.discord_state or {}),
        created_at=created_at,
        updated_at=updated_at,
    )


def _load_membership_principals(
    *,
    session: Session,
    tenant_id: str,
) -> list[TenantMembershipPrincipal]:
    principals: list[TenantMembershipPrincipal] = []
    for membership, _user in list_memberships_with_users(session=session, tenant_id=tenant_id):
        loaded_principal = load_tenant_user_principal(session=session, user_id=membership.user_id)
        membership_principal = loaded_principal.membership_for_tenant(tenant_id)
        if membership_principal is not None:
            principals.append(membership_principal)
    return principals


def _discord_client(*, session: Session) -> DiscordApiClient:
    settings = get_settings()
    token = resolve_platform_secret_ref(
        session,
        secret_ref=PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
        encryption_key=settings.secrets_encryption_key,
    )
    normalized = str(token or "").strip()
    if not normalized:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Discord bot token is not configured")
    return DiscordApiClient(bot_token=normalized)


def _resolve_discord_invite_channel_id(*, client: DiscordApiClient, discord_config: dict) -> str:
    onboarding_channel_id = str(discord_config.get("onboarding_channel_id") or "").strip()
    if onboarding_channel_id:
        return onboarding_channel_id

    channel_id = str(discord_config.get("channel_id") or "").strip()
    if channel_id:
        return channel_id

    guild_id = str(discord_config.get("guild_id") or "").strip()
    if not guild_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant Discord guild is not configured")

    try:
        channels = client.list_text_channels(guild_id=guild_id)
    except DiscordApiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    if not channels:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No Discord text channels are available for this tenant")
    return channels[0].channel_id


@router.get("/tenants", response_model=list[TenantRead])
def list_tenants(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[TenantRead]:
    if principal.is_platform_super_admin:
        return list_tenants_route_impl(
            session=session,
            tenant_model=Tenant,
            tenant_to_schema_fn=tenant_to_schema,
        )

    tenants = []
    for membership in principal.memberships:
        tenant = session.get(Tenant, membership.tenant_id)
        if tenant is not None:
            tenants.append(tenant_to_schema(tenant))
    return tenants


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
        reconcile_tenant_projects_fn=reconcile_tenant_projects,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.get("/tenants/{tenant_id}", response_model=TenantRead)
def get_tenant(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantRead:
    require_tenant_membership(principal=principal, tenant_id=tenant_id)
    return get_tenant_route_impl(
        session=session,
        tenant_id=tenant_id,
        get_tenant_or_404_fn=get_tenant_or_404_impl,
        tenant_to_schema_fn=tenant_to_schema,
    )


@router.post("/tenants/{tenant_id}/invites", response_model=TenantInviteRead, status_code=status.HTTP_201_CREATED)
def create_tenant_invite(
    tenant_id: str,
    payload: TenantInviteCreate,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantInviteRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    ensure_team_ids_exist(session=session, tenant_id=tenant_id, team_ids=payload.team_ids)
    invite, raw_token = create_invite(
        session=session,
        tenant_id=tenant_id,
        email=payload.email,
        full_name=payload.full_name,
        role=payload.role,
        team_ids=payload.team_ids,
        mode_override=payload.mode_override,
        invited_by_user_id=principal.user_id,
    )
    invite_url = _build_invite_url(request=request, raw_token=raw_token)
    _send_tenant_invite_email_or_raise(
        email=invite.email,
        full_name=invite.full_name,
        invite_url=invite_url,
        tenant_name=tenant.name,
    )
    session.commit()
    return _invite_to_schema_with_url(invite, invite_url=invite_url)


@router.get("/tenants/{tenant_id}/invites", response_model=TenantInviteListRead)
def get_tenant_invites(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantInviteListRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    return TenantInviteListRead(items=[_invite_to_schema_with_url(invite) for invite in list_invites(session=session, tenant_id=tenant_id)])


@router.post("/tenants/{tenant_id}/invites/{invite_id}/resend", response_model=TenantInviteActionResult)
def resend_tenant_invite(
    tenant_id: str,
    invite_id: str,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantInviteActionResult:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    invite = get_invite(session=session, tenant_id=tenant_id, invite_id=invite_id)
    tenant = session.get(Tenant, tenant_id)
    if invite is None or tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    next_invite, raw_token = resend_invite(session=session, invite=invite, invited_by_user_id=principal.user_id)
    invite_url = _build_invite_url(request=request, raw_token=raw_token)
    _send_tenant_invite_email_or_raise(
        email=next_invite.email,
        full_name=next_invite.full_name,
        invite_url=invite_url,
        tenant_name=tenant.name,
    )
    session.commit()
    return TenantInviteActionResult(invite=_invite_to_schema_with_url(next_invite, invite_url=invite_url))


@router.post("/tenants/{tenant_id}/invites/{invite_id}/revoke", response_model=TenantInviteActionResult)
def revoke_tenant_invite(
    tenant_id: str,
    invite_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantInviteActionResult:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    invite = get_invite(session=session, tenant_id=tenant_id, invite_id=invite_id)
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found")
    revoke_invite(session=session, invite=invite)
    session.commit()
    return TenantInviteActionResult(invite=_invite_to_schema_with_url(invite))


@router.get("/tenants/{tenant_id}/teams", response_model=list[TenantTeamRead])
def get_tenant_teams(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[TenantTeamRead]:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    return [_team_to_schema(team) for team in list_teams(session=session, tenant_id=tenant_id)]


@router.post("/tenants/{tenant_id}/teams", response_model=TenantTeamRead, status_code=status.HTTP_201_CREATED)
def create_tenant_team(
    tenant_id: str,
    payload: TenantTeamCreate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantTeamRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    unknown_permissions = sorted(set(payload.permission_keys) - KNOWN_PERMISSION_KEYS)
    if unknown_permissions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permission keys: {', '.join(unknown_permissions)}",
        )
    normalized_permission_keys = list(normalize_permission_keys(payload.permission_keys))
    team = create_team(
        session=session,
        tenant_id=tenant_id,
        name=payload.name,
        description=payload.description,
        permission_keys=normalized_permission_keys,
    )
    session.commit()
    return _team_to_schema(team)


@router.put("/tenants/{tenant_id}/teams/{team_id}", response_model=TenantTeamRead)
def update_tenant_team(
    tenant_id: str,
    team_id: str,
    payload: TenantTeamUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantTeamRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    unknown_permissions = sorted(set(payload.permission_keys) - KNOWN_PERMISSION_KEYS)
    if unknown_permissions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown permission keys: {', '.join(unknown_permissions)}",
        )
    normalized_permission_keys = list(normalize_permission_keys(payload.permission_keys))
    try:
        team = update_team(
            session=session,
            tenant_id=tenant_id,
            team_id=team_id,
            name=payload.name,
            description=payload.description,
            permission_keys=normalized_permission_keys,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    session.commit()
    return _team_to_schema(team)


@router.get("/tenants/{tenant_id}/members", response_model=list[TenantMemberRead])
def get_tenant_members(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[TenantMemberRead]:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    principals_by_id = {
        membership.membership_id: membership
        for membership in _load_membership_principals(session=session, tenant_id=tenant_id)
    }
    members: list[TenantMemberRead] = []
    for membership, user in list_memberships_with_users(session=session, tenant_id=tenant_id):
        membership_principal = principals_by_id.get(membership.membership_id)
        if membership_principal is None:
            continue
        members.append(
            _member_to_schema(
                principal=membership_principal,
                user=user,
                created_at=membership.created_at,
                updated_at=membership.updated_at,
            )
        )
    return members


@router.put("/tenants/{tenant_id}/members/{membership_id}", response_model=TenantMemberRead)
def update_tenant_member(
    tenant_id: str,
    membership_id: str,
    payload: TenantMemberUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantMemberRead:
    require_tenant_permission(
        principal=principal,
        tenant_id=tenant_id,
        permission_key=PERMISSION_PEOPLE_MANAGE,
    )
    ensure_team_ids_exist(session=session, tenant_id=tenant_id, team_ids=payload.team_ids)
    try:
        update_membership(
            session=session,
            membership_id=membership_id,
            role=payload.role,
            mode_override=payload.mode_override,
            team_ids=payload.team_ids,
            is_active=payload.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    session.commit()
    membership_principal = next(
        (
            item
            for item in _load_membership_principals(session=session, tenant_id=tenant_id)
            if item.membership_id == membership_id
        ),
        None,
    )
    if membership_principal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")
    persisted_rows = list_memberships_with_users(session=session, tenant_id=tenant_id)
    for membership, user in persisted_rows:
        if membership.membership_id == membership_id:
            return _member_to_schema(
                principal=membership_principal,
                user=user,
                created_at=membership.created_at,
                updated_at=membership.updated_at,
            )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Membership not found")


@router.get("/tenants/{tenant_id}/discord/identity", response_model=TenantDiscordIdentityRead)
def get_tenant_discord_identity(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantDiscordIdentityRead:
    membership = require_tenant_membership(principal=principal, tenant_id=tenant_id)
    settings = get_settings()
    oauth_config = resolve_discord_oauth_config(session=session, settings=settings)
    oauth_configured = discord_oauth_is_configured(config=oauth_config)
    if principal.user_id is None or membership is None:
        return TenantDiscordIdentityRead(linked=False, oauth_configured=oauth_configured)
    identity = get_discord_identity(session=session, user_id=principal.user_id)
    if identity is None:
        return TenantDiscordIdentityRead(linked=False, oauth_configured=oauth_configured)
    return TenantDiscordIdentityRead(
        oauth_configured=oauth_configured,
        linked=True,
        discord_user_id=identity.discord_user_id,
        discord_username=identity.discord_username,
        discord_global_name=identity.discord_global_name,
        discord_avatar_hash=identity.discord_avatar_hash,
        linked_at=identity.linked_at,
    )


@router.post("/tenants/{tenant_id}/discord/link/start", response_model=TenantDiscordLinkStartRead)
def start_tenant_discord_link(
    tenant_id: str,
    redirect_to: str = Query(default="/get-started"),
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantDiscordLinkStartRead:
    require_tenant_membership(principal=principal, tenant_id=tenant_id)
    if principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant user context required")
    settings = get_settings()
    oauth_config = resolve_discord_oauth_config(session=session, settings=settings)
    state = issue_discord_oauth_state(
        settings=settings,
        tenant_id=tenant_id,
        user_id=principal.user_id,
        redirect_to=redirect_to,
    )
    try:
        authorize_url = build_discord_oauth_authorize_url(config=oauth_config, state=state)
    except DiscordOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return TenantDiscordLinkStartRead(authorize_url=authorize_url)


@router.post("/tenants/{tenant_id}/discord/onboarding-invite", response_model=TenantDiscordInviteRead)
def create_tenant_discord_onboarding_invite(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantDiscordInviteRead:
    membership = require_tenant_membership(principal=principal, tenant_id=tenant_id)
    tenant = session.get(Tenant, tenant_id)
    if tenant is None or membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    if principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant user context required")
    discord_config = dict(tenant.discord_config or {})
    client = _discord_client(session=session)
    channel_id = _resolve_discord_invite_channel_id(client=client, discord_config=discord_config)
    expires_in_seconds = discord_config.get("onboarding_invite_expires_in_seconds")
    max_uses = discord_config.get("onboarding_invite_max_uses")
    try:
        invite = client.create_invite(
            channel_id=channel_id,
            max_age=int(expires_in_seconds) if expires_in_seconds is not None else None,
            max_uses=int(max_uses) if max_uses is not None else None,
        )
    except (DiscordApiError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    code = str(invite.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Discord invite response missing code")
    expires_at = None
    expires_at_raw = str(invite.get("expires_at") or "").strip()
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(expires_at_raw.replace("Z", "+00:00"))
        except ValueError:
            expires_at = None
    if expires_at is None and expires_in_seconds is not None:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in_seconds))
    update_membership_discord_state(
        session=session,
        membership_id=membership.membership_id,
        mutate=lambda state: state.update(
            {
                "linked": bool(get_discord_identity(session=session, user_id=principal.user_id)),
                "invite_generated": True,
                "invite_generated_at": datetime.now(UTC).isoformat(),
                "guild_joined": bool(state.get("guild_joined")),
                "welcome_status": str(state.get("welcome_status") or "pending"),
                "last_failure_reason": state.get("last_failure_reason"),
            }
        ),
    )
    session.commit()
    return TenantDiscordInviteRead(
        invite_url=f"https://discord.gg/{code}",
        expires_at=expires_at,
        max_uses=int(max_uses) if max_uses is not None else None,
    )


@router.get("/tenants/{tenant_id}/delivery-summary", response_model=TenantDeliverySummaryRead)
def get_tenant_delivery_summary(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> TenantDeliverySummaryRead:
    require_tenant_membership(principal=principal, tenant_id=tenant_id)
    summary = summarize_delivery(session=session, tenant_id=tenant_id)
    return TenantDeliverySummaryRead(
        summary={
            "completed_count": summary.completed_count,
            "in_review_count": summary.in_review_count,
            "blocked_count": summary.blocked_count,
            "failed_count": summary.failed_count,
            "queued_count": summary.queued_count,
            "median_cycle_time_hours": summary.median_cycle_time_hours,
            "average_cycle_time_hours": summary.average_cycle_time_hours,
        },
        timeline=[
            {
                "run_id": run.run_id,
                "project_id": run.project_id,
                "issue_key": run.issue_key,
                "issue_summary": run.issue_summary,
                "status": run.status,
                "completed_at": run.finished_at,
                "started_at": run.started_at,
                "pr_url": run.pr_url,
            }
            for run in summary.timeline
        ],
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
    settings = get_settings()
    return set_tenant_archive_state_route_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=False,
        set_tenant_archive_state_fn=set_tenant_archive_state_impl,
        tenant_to_schema_fn=tenant_to_schema,
        archive_retention_days=settings.tenant_archive_retention_days,
    )


@router.post("/tenants/{tenant_id}/unarchive", response_model=TenantRead)
def unarchive_tenant(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> TenantRead:
    settings = get_settings()
    return set_tenant_archive_state_route_impl(
        session=session,
        tenant_id=tenant_id,
        is_enabled=True,
        set_tenant_archive_state_fn=set_tenant_archive_state_impl,
        tenant_to_schema_fn=tenant_to_schema,
        archive_retention_days=settings.tenant_archive_retention_days,
    )


@router.get("/tenants/{tenant_id}/projects", response_model=list[ProjectRead])
def list_projects(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> list[ProjectRead]:
    require_tenant_membership(principal=principal, tenant_id=tenant_id)
    return list_projects_route_impl(
        session=session,
        tenant_id=tenant_id,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.post("/tenants/{tenant_id}/projects", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    tenant_id: str,
    payload: ProjectCreate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
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
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_membership(principal=principal, tenant_id=tenant_id)
    return get_project_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.patch("/tenants/{tenant_id}/projects/{project_id}/configuration", response_model=ProjectRead)
def update_project_configuration(
    tenant_id: str,
    project_id: str,
    payload: ProjectConfigurationUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    return update_project_configuration_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.patch("/tenants/{tenant_id}/projects/{project_id}/policy", response_model=ProjectRead)
def update_project_policy(
    tenant_id: str,
    project_id: str,
    payload: ProjectPolicyUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    return update_project_policy_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.patch("/tenants/{tenant_id}/projects/{project_id}/environment", response_model=ProjectRead)
def update_project_environment(
    tenant_id: str,
    project_id: str,
    payload: ProjectEnvironmentUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    return update_project_environment_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.patch("/tenants/{tenant_id}/projects/{project_id}/secrets", response_model=ProjectRead)
def update_project_secret_refs(
    tenant_id: str,
    project_id: str,
    payload: ProjectSecretRefsUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    return update_project_secret_refs_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.patch("/tenants/{tenant_id}/projects/{project_id}/discord", response_model=ProjectRead)
def update_project_discord(
    tenant_id: str,
    project_id: str,
    payload: ProjectDiscordUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    return update_project_discord_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.patch("/tenants/{tenant_id}/projects/{project_id}/archive", response_model=ProjectRead)
def update_project_archive_state(
    tenant_id: str,
    project_id: str,
    payload: ProjectArchiveUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    return update_project_archive_state_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        payload=payload,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.post("/tenants/{tenant_id}/projects/{project_id}/jira/resolve-run-board", response_model=ProjectRead)
def resolve_project_jira_run_board(
    tenant_id: str,
    project_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    return resolve_project_jira_run_board_route_impl(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        admin_project_service_factory=admin_project_service,
    )  # type: ignore[return-value]


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/installs",
    response_model=ProjectInstallsRead,
)
def get_project_installs(
    tenant_id: str,
    project_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectInstallsRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    project = _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    installs = list_project_installs(session=session, tenant_id=tenant_id, project_id=project.project_id)
    return ProjectInstallsRead(installs=[project_install_to_schema(install) for install in installs])


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/installs",
    response_model=ProjectInstallRead,
    status_code=status.HTTP_201_CREATED,
)
def create_project_install_route(
    tenant_id: str,
    project_id: str,
    payload: ProjectInstallWrite,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectInstallRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    project = _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    try:
        install = create_project_install(
            session=session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            payload=ServiceProjectInstallWrite(
                kind=payload.kind,
                label=payload.label,
                enabled=payload.enabled,
                config=payload.config,
                binding_names=tuple(payload.binding_names),
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return project_install_to_schema(install)


@router.put(
    "/tenants/{tenant_id}/projects/{project_id}/installs/{install_id}",
    response_model=ProjectInstallRead,
)
def update_project_install_route(
    tenant_id: str,
    project_id: str,
    install_id: str,
    payload: ProjectInstallWrite,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectInstallRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    install = get_project_install(session=session, install_id=install_id)
    if install is None or install.tenant_id != tenant_id or install.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project install not found")
    try:
        updated = update_project_install(
            session=session,
            install=install,
            payload=ServiceProjectInstallWrite(
                kind=payload.kind,
                label=payload.label,
                enabled=payload.enabled,
                config=payload.config,
                binding_names=tuple(payload.binding_names),
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return project_install_to_schema(updated)


@router.delete(
    "/tenants/{tenant_id}/projects/{project_id}/installs/{install_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_project_install_route(
    tenant_id: str,
    project_id: str,
    install_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> Response:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    install = get_project_install(session=session, install_id=install_id)
    if install is None or install.tenant_id != tenant_id or install.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project install not found")
    delete_project_install(session=session, install=install)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/install-requests",
    response_model=ProjectInstallRequestsRead,
)
def get_project_install_requests_route(
    tenant_id: str,
    project_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectInstallRequestsRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    project = _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    requests = list_project_install_requests(session=session, tenant_id=tenant_id, project_id=project.project_id)
    return ProjectInstallRequestsRead(requests=[project_install_request_to_schema(request) for request in requests])


@router.put(
    "/tenants/{tenant_id}/projects/{project_id}/install-requests/{request_id}",
    response_model=ProjectInstallRequestRead,
)
def update_project_install_request_route(
    tenant_id: str,
    project_id: str,
    request_id: str,
    payload: ProjectInstallRequestUpdate,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> ProjectInstallRequestRead:
    require_tenant_permission(principal=principal, tenant_id=tenant_id, permission_key=PERMISSION_PROJECTS_MANAGE)
    project = _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    request = get_project_install_request(session=session, request_id=request_id)
    if request is None or request.tenant_id != tenant_id or request.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project install request not found")
    normalized_status = str(payload.status or "").strip().lower()
    settings = get_settings()
    try:
        if normalized_status == INSTALL_REQUEST_STATUS_APPROVED:
            updated = approve_install_request(
                session=session,
                settings=settings,
                request=request,
                project=project,
                source_ref=f"admin:{principal.user_id or principal.username or principal.principal_type}",
            )
            return project_install_request_to_schema(updated)
        if normalized_status == INSTALL_REQUEST_STATUS_REJECTED:
            updated = reject_install_request(
                session=session,
                settings=settings,
                request=request,
                source_ref=f"admin:{principal.user_id or principal.username or principal.principal_type}",
            )
            return project_install_request_to_schema(updated)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if normalized_status == "fulfilled":
        matching_install = next(
            (
                install
                for install in list_project_installs(session=session, tenant_id=tenant_id, project_id=project_id)
                if install.kind == request.kind and install.label == request.label
            ),
            None,
        )
        if matching_install is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Create a matching project install before marking this request fulfilled",
            )
    try:
        updated = update_install_request_status(session=session, request=request, status=normalized_status)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return project_install_request_to_schema(updated)


def _get_project_for_tenant_or_404(*, session: Session, tenant_id: str, project_id: str) -> Project:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _automation_execution_to_schema(execution) -> ProjectAutomationExecutionRead:  # noqa: ANN001
    return ProjectAutomationExecutionRead(
        execution_id=execution.execution_id,
        automation_id=execution.automation_id,
        scheduled_for=execution.scheduled_for,
        window_start_at=execution.window_start_at,
        window_end_at=execution.window_end_at,
        status=execution.status,
        dedupe_key=execution.dedupe_key,
        started_at=execution.started_at,
        completed_at=execution.completed_at,
        discord_message_id=execution.discord_message_id,
        last_error=execution.last_error,
        created_at=execution.created_at,
        updated_at=execution.updated_at,
    )


def _automation_to_schema(*, automation, executions) -> ProjectAutomationRead:  # noqa: ANN001
    return ProjectAutomationRead(
        automation_id=automation.automation_id,
        project_id=automation.project_id,
        tenant_id=automation.tenant_id,
        enabled=automation.enabled,
        kind=automation.kind,
        timezone=automation.timezone,
        days_of_week=list(automation.days_of_week or []),
        local_time=automation.local_time,
        fallback_lookback_hours=automation.fallback_lookback_hours,
        last_successful_window_end_at=automation.last_successful_window_end_at,
        next_run_at=automation.next_run_at,
        executions=[_automation_execution_to_schema(execution) for execution in executions],
        created_at=automation.created_at,
        updated_at=automation.updated_at,
    )


@router.get(
    "/tenants/{tenant_id}/projects/{project_id}/automations",
    response_model=ProjectAutomationsRead,
)
def get_project_automations(
    tenant_id: str,
    project_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectAutomationsRead:
    project = _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    automations = list_project_automation_definitions(
        session=session,
        tenant_id=tenant_id,
        project_id=project.project_id,
    )
    return ProjectAutomationsRead(
        automations=[
            _automation_to_schema(
                automation=automation,
                executions=list_execution_history(
                    session=session,
                    automation_id=automation.automation_id,
                    limit=20,
                ),
            )
            for automation in automations
        ]
    )


@router.put(
    "/tenants/{tenant_id}/projects/{project_id}/automations",
    response_model=ProjectAutomationsRead,
)
def put_project_automations(
    tenant_id: str,
    project_id: str,
    payload: ProjectAutomationsWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectAutomationsRead:
    project = _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    for item in payload.automations:
        normalized = ServiceProjectAutomationWrite(
            kind=item.kind,
            enabled=item.enabled,
            timezone=item.timezone,
            days_of_week=tuple(int(day) for day in item.days_of_week),
            local_time=item.local_time,
            fallback_lookback_hours=item.fallback_lookback_hours,
        )
        try:
            upsert_project_automation(
                session=session,
                tenant_id=tenant_id,
                project_id=project.project_id,
                payload=normalized,
            )
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    automations = list_project_automation_definitions(
        session=session,
        tenant_id=tenant_id,
        project_id=project.project_id,
    )
    return ProjectAutomationsRead(
        automations=[
            _automation_to_schema(
                automation=automation,
                executions=list_execution_history(
                    session=session,
                    automation_id=automation.automation_id,
                    limit=20,
                ),
            )
            for automation in automations
        ]
    )


@router.post(
    "/tenants/{tenant_id}/projects/{project_id}/automations/{kind}/run-now",
    response_model=ProjectAutomationsRead,
)
def post_project_automation_run_now(
    tenant_id: str,
    project_id: str,
    kind: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ProjectAutomationsRead:
    project = _get_project_for_tenant_or_404(session=session, tenant_id=tenant_id, project_id=project_id)
    try:
        enqueue_project_automation_run_now(
            session=session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            kind=kind,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    automations = list_project_automation_definitions(
        session=session,
        tenant_id=tenant_id,
        project_id=project.project_id,
    )
    return ProjectAutomationsRead(
        automations=[
            _automation_to_schema(
                automation=automation,
                executions=list_execution_history(
                    session=session,
                    automation_id=automation.automation_id,
                    limit=20,
                ),
            )
            for automation in automations
        ]
    )
