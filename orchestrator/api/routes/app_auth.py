from __future__ import annotations

from datetime import UTC
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.api.admin.route_helpers import (
    allocate_tenant_id,
    ensure_default_project_for_tenant,
    sync_tenant_jira_project_keys,
    validate_codex_assets_for_tenant_init,
)
from orchestrator.api.admin.schema_mappers import tenant_to_schema
from orchestrator.api.dependencies import get_session
from orchestrator.api.url_helpers import resolve_public_base_url
from pydantic import BaseModel, Field

from orchestrator.api.schemas import (
    AuthenticatedPrincipalRead,
    PublicRegistrationRequest,
    PublicRegistrationResponse,
    TenantInviteRead,
    TenantMembershipIdentityRead,
    TenantUserLoginRequest,
    TenantUserLoginResponse,
)
from orchestrator.core.auth_tokens import create_auth_access_token
from orchestrator.core.config import get_settings
from orchestrator.core.discord.oauth import DiscordOAuthError, exchange_code_for_user, parse_discord_oauth_state
from orchestrator.core.password_reset_email import send_password_reset_email
from orchestrator.core.password_reset_tokens import (
    PasswordResetTokenError,
    issue_password_reset_token,
    normalize_password_reset_timestamp,
    parse_password_reset_token,
)
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    TenantMembershipPrincipal,
    change_platform_admin_password,
    load_tenant_user_principal,
    require_authenticated_principal,
    require_tenant_membership,
)
from orchestrator.core.tenant_users import (
    accept_invite,
    authenticate_tenant_user,
    change_user_password,
    create_membership,
    create_tenant_record,
    create_tenant_user,
    find_tenant_user_by_email,
    link_discord_identity,
    mark_membership_signed_in,
    normalize_email,
    reset_user_password,
    resolve_invite,
    utcnow,
    update_membership_mode_override,
    update_membership_discord_state,
    update_user_profile,
)
from orchestrator.storage.models import Tenant, TenantMembership, TenantUser, TenantUserCredential
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError


router = APIRouter(tags=["app-auth"])

PASSWORD_RESET_SENT_MESSAGE = "If an account exists for that email, a reset link has been sent."


def _membership_to_schema(membership: TenantMembershipPrincipal) -> TenantMembershipIdentityRead:
    return TenantMembershipIdentityRead(
        membership_id=membership.membership_id,
        tenant_id=membership.tenant_id,
        role=membership.role,
        permission_keys=list(membership.permission_keys),
        effective_mode=membership.effective_mode,
        mode_override=membership.mode_override,
        onboarding_kind=membership.onboarding_kind,
        first_signed_in_at=membership.first_signed_in_at,
        onboarding_completed_at=membership.onboarding_completed_at,
        onboarding_version=membership.onboarding_version,
        team_ids=list(membership.team_ids),
        discord_state=dict(membership.discord_state or {}),
    )


def _principal_to_schema(principal: AuthenticatedPrincipal) -> AuthenticatedPrincipalRead:
    return AuthenticatedPrincipalRead(
        principal_type=principal.principal_type,
        username=principal.username,
        user_id=principal.user_id,
        email=principal.email,
        full_name=principal.full_name,
        memberships=[_membership_to_schema(membership) for membership in principal.memberships],
    )


def _build_login_response(*, principal: AuthenticatedPrincipal) -> TenantUserLoginResponse:
    settings = get_settings()
    if principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Missing tenant principal")
    token, expires_in = create_auth_access_token(
        user_id=principal.user_id,
        secret=settings.auth_token_secret,
        ttl_seconds=settings.auth_token_ttl_seconds,
    )
    return TenantUserLoginResponse(
        access_token=token,
        expires_in=expires_in,
        principal=_principal_to_schema(principal),
    )


def _password_reset_secret(*, settings) -> str:  # noqa: ANN001
    return str(settings.auth_token_secret).strip()


class PublicMessageResponse(BaseModel):
    detail: str


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class PasswordResetConfirmRequest(BaseModel):
    token: str = Field(min_length=1)
    new_password: str = Field(min_length=8)
@router.post("/api/public/register", response_model=PublicRegistrationResponse, status_code=status.HTTP_201_CREATED)
def public_register(
    payload: PublicRegistrationRequest,
    session: Session = Depends(get_session),
) -> PublicRegistrationResponse:
    validate_codex_assets_for_tenant_init()
    existing = session.execute(
        select(TenantUser).where(TenantUser.email == normalize_email(payload.email))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")

    tenant_id = allocate_tenant_id(session, name=payload.tenant_name)
    tenant = create_tenant_record(tenant_id=tenant_id, tenant_name=payload.tenant_name)
    tenant_user = create_tenant_user(
        session=session,
        email=payload.email,
        full_name=payload.full_name,
        password=payload.password,
    )
    create_membership(
        session=session,
        tenant_id=tenant.tenant_id,
        user_id=tenant_user.user_id,
        role="tenant_admin",
        mode_override="technical",
        onboarding_kind="tenant_admin_setup",
    )
    session.add(tenant)
    ensure_default_project_for_tenant(session, tenant=tenant)
    sync_tenant_jira_project_keys(session, tenant=tenant)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Unable to create tenant account") from exc
    principal = load_tenant_user_principal(session=session, user_id=tenant_user.user_id)
    login = _build_login_response(principal=principal)
    persisted_tenant = session.get(Tenant, tenant.tenant_id)
    if persisted_tenant is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Tenant creation failed")
    return PublicRegistrationResponse(
        access_token=login.access_token,
        expires_in=login.expires_in,
        principal=login.principal,
        tenant=tenant_to_schema(persisted_tenant),
    )


@router.post(
    "/api/public/password-reset/request",
    response_model=PublicMessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def request_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> PublicMessageResponse:
    settings = get_settings()
    tenant_user = find_tenant_user_by_email(session=session, email=payload.email)
    if tenant_user is not None and tenant_user.is_active:
        credential = session.get(TenantUserCredential, tenant_user.user_id)
        if credential is not None:
            token = issue_password_reset_token(
                user_id=tenant_user.user_id,
                email=tenant_user.email,
                password_updated_at=credential.password_updated_at,
                secret=_password_reset_secret(settings=settings),
            )
            base = resolve_public_base_url(request=request, configured_base_url=settings.admin_ui_base_url)
            reset_url = f"{base}/reset-password?token={quote(token)}"
            send_password_reset_email(
                email=tenant_user.email,
                full_name=tenant_user.full_name,
                reset_url=reset_url,
            )
    return PublicMessageResponse(detail=PASSWORD_RESET_SENT_MESSAGE)


@router.post("/api/public/password-reset/confirm", response_model=PublicMessageResponse)
def confirm_password_reset(
    payload: PasswordResetConfirmRequest,
    session: Session = Depends(get_session),
) -> PublicMessageResponse:
    settings = get_settings()
    try:
        token_payload = parse_password_reset_token(
            token=payload.token,
            secret=_password_reset_secret(settings=settings),
        )
    except PasswordResetTokenError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    tenant_user = session.get(TenantUser, token_payload.user_id)
    if tenant_user is None or not tenant_user.is_active or normalize_email(tenant_user.email) != token_payload.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid password reset token")
    credential = session.get(TenantUserCredential, tenant_user.user_id)
    if credential is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid password reset token")
    if normalize_password_reset_timestamp(credential.password_updated_at) != token_payload.password_updated_at:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid password reset token")
    reset_user_password(session=session, user_id=tenant_user.user_id, new_password=payload.new_password)
    session.commit()
    return PublicMessageResponse(detail="Password updated. You can now sign in.")


@router.post("/api/app/auth/login", response_model=TenantUserLoginResponse)
def tenant_user_login(
    payload: TenantUserLoginRequest,
    session: Session = Depends(get_session),
) -> TenantUserLoginResponse:
    tenant_user = authenticate_tenant_user(session=session, email=payload.email, password=payload.password)
    if tenant_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid tenant credentials")
    mark_membership_signed_in(session=session, user_id=tenant_user.user_id)
    session.commit()
    principal = load_tenant_user_principal(session=session, user_id=tenant_user.user_id)
    return _build_login_response(principal=principal)


@router.get("/api/app/auth/me", response_model=AuthenticatedPrincipalRead)
def tenant_user_me(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> AuthenticatedPrincipalRead:
    return _principal_to_schema(principal)


class TenantUserProfileUpdateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)


@router.put("/api/app/me/profile", response_model=AuthenticatedPrincipalRead)
def update_authenticated_user_profile(
    payload: TenantUserProfileUpdateRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> AuthenticatedPrincipalRead:
    if principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        update_user_profile(session=session, user_id=principal.user_id, full_name=payload.full_name)
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    refreshed = load_tenant_user_principal(session=session, user_id=principal.user_id)
    return _principal_to_schema(refreshed)


class TenantUserSettingsUpdateRequest(BaseModel):
    mode_override: str | None = Field(default=None, pattern="^(technical|non_technical)$")


@router.put("/api/app/tenants/{tenant_id}/me/settings", response_model=AuthenticatedPrincipalRead)
def update_tenant_user_settings(
    tenant_id: str,
    payload: TenantUserSettingsUpdateRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> AuthenticatedPrincipalRead:
    membership = require_tenant_membership(principal=principal, tenant_id=tenant_id)
    if membership is None or principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant membership not found")
    try:
        update_membership_mode_override(
            session=session,
            membership_id=membership.membership_id,
            role=membership.role,
            permission_keys=list(membership.permission_keys),
            mode_override=payload.mode_override,
        )
        session.commit()
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    refreshed = load_tenant_user_principal(session=session, user_id=principal.user_id)
    return _principal_to_schema(refreshed)


class TenantUserPasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


@router.post("/api/app/me/password", response_model=AuthenticatedPrincipalRead)
def change_authenticated_user_password(
    payload: TenantUserPasswordChangeRequest,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> AuthenticatedPrincipalRead:
    if principal.is_platform_super_admin:
        if principal.username is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        try:
            change_platform_admin_password(
                session=session,
                username=principal.username,
                current_password=payload.current_password,
                new_password=payload.new_password,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
        return _principal_to_schema(principal)

    if principal.user_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        change_user_password(
            session=session,
            user_id=principal.user_id,
            current_password=payload.current_password,
            new_password=payload.new_password,
        )
        session.commit()
    except PermissionError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    refreshed = load_tenant_user_principal(session=session, user_id=principal.user_id)
    return _principal_to_schema(refreshed)


class InviteAcceptRequest(BaseModel):
    token: str = Field(min_length=1)
    password: str = Field(min_length=8)
    full_name: str | None = Field(default=None, max_length=255)


@router.post("/api/public/invites/accept", response_model=TenantUserLoginResponse)
def accept_public_invite(
    payload: InviteAcceptRequest,
    session: Session = Depends(get_session),
) -> TenantUserLoginResponse:
    invite = resolve_invite(session=session, raw_token=payload.token)
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invite not found or expired")
    tenant_user = accept_invite(
        session=session,
        invite=invite,
        password=payload.password,
        full_name=payload.full_name,
    )
    mark_membership_signed_in(session=session, user_id=tenant_user.user_id)
    session.commit()
    principal = load_tenant_user_principal(session=session, user_id=tenant_user.user_id)
    return _build_login_response(principal=principal)


def invite_to_schema(invite, *, invite_url: str | None = None) -> TenantInviteRead:  # noqa: ANN001
    return TenantInviteRead(
        invite_id=invite.invite_id,
        tenant_id=invite.tenant_id,
        email=invite.email,
        full_name=invite.full_name,
        role=invite.role,
        team_ids=list(invite.team_ids or []),
        mode_override=invite.mode_override,
        status=invite.status,
        invite_url=invite_url,
        expires_at=invite.expires_at,
        accepted_at=invite.accepted_at,
        revoked_at=invite.revoked_at,
        created_at=invite.created_at,
        updated_at=invite.updated_at,
    )


@router.post("/api/app/onboarding/{tenant_id}/complete", response_model=AuthenticatedPrincipalRead)
def complete_onboarding(
    tenant_id: str,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> AuthenticatedPrincipalRead:
    membership = require_tenant_membership(principal=principal, tenant_id=tenant_id)
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant membership not found")
    persisted_membership = session.get(TenantMembership, membership.membership_id)
    if persisted_membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant membership not found")
    now = utcnow()
    persisted_membership.onboarding_completed_at = now
    persisted_membership.updated_at = now
    tenant = session.get(Tenant, tenant_id)
    if tenant is not None and persisted_membership.onboarding_kind == "tenant_admin_setup":
        setup_state = dict(tenant.setup_state or {})
        setup_state["onboarding_completed_at"] = now.isoformat()
        tenant.setup_state = setup_state
        tenant.updated_at = now
    session.commit()
    refreshed_principal = load_tenant_user_principal(session=session, user_id=principal.user_id or "")
    return _principal_to_schema(refreshed_principal)


@router.get("/api/app/discord/callback")
def discord_oauth_callback(
    code: str,
    state: str,
    session: Session = Depends(get_session),
) -> RedirectResponse:
    settings = get_settings()
    try:
        parsed_state = parse_discord_oauth_state(settings=settings, state=state)
        discord_user = exchange_code_for_user(settings=settings, code=code)
    except DiscordOAuthError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    membership = session.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == parsed_state.tenant_id,
            TenantMembership.user_id == parsed_state.user_id,
        )
    ).scalar_one_or_none()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant membership not found")

    link_discord_identity(
        session=session,
        user_id=parsed_state.user_id,
        discord_user_id=discord_user.discord_user_id,
        discord_username=discord_user.username,
        discord_global_name=discord_user.global_name,
        discord_avatar_hash=discord_user.avatar_hash,
    )

    tenant = session.get(Tenant, parsed_state.tenant_id)
    discord_config = dict((tenant.discord_config if tenant is not None else None) or {})
    guild_id = str(discord_config.get("guild_id") or "").strip()
    join_failure: str | None = None
    if guild_id:
        bot_token = resolve_platform_secret_ref(
            session,
            secret_ref=PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
            encryption_key=settings.secrets_encryption_key,
        )
        if str(bot_token or "").strip():
            try:
                DiscordApiClient(bot_token=str(bot_token)).add_guild_member(
                    guild_id=guild_id,
                    user_id=discord_user.discord_user_id,
                    user_access_token=discord_user.access_token,
                )
            except (DiscordApiError, ValueError) as exc:
                join_failure = str(exc)

    def mutate_discord_state(state_payload: dict) -> None:
        state_payload["linked"] = True
        state_payload["linked_at"] = utcnow().isoformat()
        if join_failure:
            state_payload["last_failure_reason"] = join_failure
        elif guild_id:
            state_payload["join_requested_at"] = utcnow().isoformat()

    update_membership_discord_state(
        session=session,
        membership_id=membership.membership_id,
        mutate=mutate_discord_state,
    )
    session.commit()
    return RedirectResponse(url=f"{settings.admin_ui_base_url.rstrip('/')}{parsed_state.redirect_to}", status_code=status.HTTP_302_FOUND)
