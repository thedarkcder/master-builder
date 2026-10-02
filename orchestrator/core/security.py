from __future__ import annotations

from dataclasses import dataclass
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import (
    HTTPAuthorizationCredentials,
    HTTPBasic,
    HTTPBasicCredentials,
    HTTPBearer,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform.admin_tokens import parse_admin_access_token
from orchestrator.core.platform.auth_tokens import parse_auth_access_token
from orchestrator.core.config import get_settings
from orchestrator.core.deployment_host_tokens import hash_deployment_host_token
from orchestrator.core.platform.passwords import hash_password, verify_password
from orchestrator.core.platform.secret_service import (
    PLATFORM_SECRET_ADMIN_PASSWORD_HASH_REF,
    platform_secret_service,
    resolve_platform_secret_ref,
)
from orchestrator.core.platform.access import (
    MODE_TECHNICAL,
    VALID_ROLE_KEYS,
    compute_permission_snapshot,
    normalize_permission_key,
)
from orchestrator.api.dependencies import get_session
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import (
    Tenant,
    TenantMembership,
    TenantTeam,
    TenantTeamMembership,
    TenantUser,
    DeploymentHost,
)
from orchestrator.storage.tenant_rls import (
    set_platform_admin_rls_context,
    set_platform_system_rls_context,
    set_tenant_user_rls_context,
)

basic_auth = HTTPBasic(auto_error=False)
bearer_auth = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class TenantMembershipPrincipal:
    membership_id: str
    tenant_id: str
    role: str
    permission_keys: tuple[str, ...]
    effective_mode: str
    mode_override: str | None
    onboarding_kind: str
    first_signed_in_at: object | None
    onboarding_completed_at: object | None
    onboarding_version: str | None
    team_ids: tuple[str, ...]
    discord_state: dict


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    principal_type: str
    username: str | None = None
    user_id: str | None = None
    email: str | None = None
    full_name: str | None = None
    memberships: tuple[TenantMembershipPrincipal, ...] = ()

    @property
    def is_platform_super_admin(self) -> bool:
        return self.principal_type == "platform_super_admin"

    def membership_for_tenant(self, tenant_id: str) -> TenantMembershipPrincipal | None:
        for membership in self.memberships:
            if membership.tenant_id == tenant_id:
                return membership
        return None


@dataclass(frozen=True)
class DeploymentHostPrincipal:
    host_id: str
    label: str


def _resolve_admin_password_hash(*, session: Session) -> str | None:
    settings = get_settings()
    encryption_key = settings.secrets_encryption_key.strip()
    if not encryption_key:
        return None
    return resolve_platform_secret_ref(
        session,
        secret_ref=PLATFORM_SECRET_ADMIN_PASSWORD_HASH_REF,
        encryption_key=encryption_key,
        allow_environment_fallback=False,
    )


def validate_admin_credentials(
    *, session: Session, username: str, password: str
) -> bool:
    settings = get_settings()
    valid_user = secrets.compare_digest(username, settings.admin_username)
    if not valid_user:
        return False

    password_hash = _resolve_admin_password_hash(session=session)
    if password_hash:
        return verify_password(password=password, password_hash=password_hash)
    return secrets.compare_digest(password, settings.admin_password)


def change_platform_admin_password(
    *, session: Session, username: str, current_password: str, new_password: str
) -> None:
    settings = get_settings()
    if not validate_admin_credentials(
        session=session, username=username, password=current_password
    ):
        raise PermissionError("Current password is incorrect")
    if len(new_password) < 8:
        raise ValueError("New password must be at least eight characters")
    encryption_key = settings.secrets_encryption_key.strip()
    if not encryption_key:
        raise RuntimeError("Platform password management is unavailable")
    platform_secret_service.upsert_secret(
        session=session,
        secret_ref=PLATFORM_SECRET_ADMIN_PASSWORD_HASH_REF,
        plaintext_value=hash_password(new_password),
        encryption_key=encryption_key,
    )


def _admin_unauthorized(detail: str = "Invalid admin credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer, Basic"},
    )


def _build_platform_admin_principal(username: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        principal_type="platform_super_admin", username=username
    )


def _load_tenant_memberships(
    *, session: Session, user_id: str
) -> tuple[TenantMembershipPrincipal, ...]:
    membership_rows = session.execute(
        select(TenantMembership, Tenant)
        .join(Tenant, Tenant.tenant_id == TenantMembership.tenant_id)
        .where(TenantMembership.user_id == user_id)
        .order_by(
            TenantMembership.created_at.asc(), TenantMembership.membership_id.asc()
        )
    ).all()
    memberships: list[TenantMembershipPrincipal] = []
    for membership, tenant in membership_rows:
        team_rows = session.execute(
            select(TenantTeam.team_id, TenantTeam.permission_keys)
            .join(
                TenantTeamMembership, TenantTeamMembership.team_id == TenantTeam.team_id
            )
            .where(TenantTeamMembership.membership_id == membership.membership_id)
            .order_by(TenantTeam.created_at.asc(), TenantTeam.team_id.asc())
        ).all()
        team_ids = [str(team_id) for team_id, _ in team_rows]
        team_permissions: list[str] = []
        for _, permission_keys in team_rows:
            if isinstance(permission_keys, list):
                team_permissions.extend(
                    str(permission) for permission in permission_keys
                )
        tenant_default_mode = str(
            (tenant.experience_config or {}).get("default_mode") or MODE_TECHNICAL
        )
        snapshot = compute_permission_snapshot(
            tenant_default_mode=tenant_default_mode,
            membership_role=membership.role,
            membership_mode_override=membership.mode_override,
            team_permission_keys=team_permissions,
        )
        memberships.append(
            TenantMembershipPrincipal(
                membership_id=membership.membership_id,
                tenant_id=membership.tenant_id,
                role=membership.role,
                permission_keys=snapshot.permission_keys,
                effective_mode=snapshot.effective_mode,
                mode_override=membership.mode_override,
                onboarding_kind=membership.onboarding_kind,
                first_signed_in_at=membership.first_signed_in_at,
                onboarding_completed_at=membership.onboarding_completed_at,
                onboarding_version=membership.onboarding_version,
                team_ids=tuple(team_ids),
                discord_state=dict(membership.discord_state or {}),
            )
        )
    return tuple(memberships)


def load_tenant_user_principal(
    *, session: Session, user_id: str
) -> AuthenticatedPrincipal:
    set_tenant_user_rls_context(session=session, user_id=user_id)
    tenant_user = session.get(TenantUser, user_id)
    if tenant_user is None or not tenant_user.is_active:
        raise _admin_unauthorized("Invalid tenant credentials")
    memberships = _load_tenant_memberships(session=session, user_id=tenant_user.user_id)
    if not memberships:
        raise _admin_unauthorized("No tenant memberships found")
    return AuthenticatedPrincipal(
        principal_type="tenant_user",
        user_id=tenant_user.user_id,
        email=tenant_user.email,
        full_name=tenant_user.full_name,
        memberships=memberships,
    )


def require_authenticated_principal(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(bearer_auth),
    basic_credentials: HTTPBasicCredentials | None = Depends(basic_auth),
    session: Session = Depends(get_session),
) -> AuthenticatedPrincipal:
    return _authenticate_principal(
        bearer_credentials=bearer_credentials,
        basic_credentials=basic_credentials,
        session=session,
    )


def require_authenticated_stream_principal(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(bearer_auth),
    basic_credentials: HTTPBasicCredentials | None = Depends(basic_auth),
) -> AuthenticatedPrincipal:
    session_factory = create_session_factory()
    with session_factory() as session:
        return _authenticate_principal(
            bearer_credentials=bearer_credentials,
            basic_credentials=basic_credentials,
            session=session,
        )


def _authenticate_principal(
    *,
    bearer_credentials: HTTPAuthorizationCredentials | None,
    basic_credentials: HTTPBasicCredentials | None,
    session: Session,
) -> AuthenticatedPrincipal:
    settings = get_settings()

    if bearer_credentials is not None and bearer_credentials.scheme.lower() == "bearer":
        token = bearer_credentials.credentials
        try:
            username = parse_admin_access_token(
                token=token, secret=settings.admin_token_secret
            )
        except ValueError:
            try:
                user_id = parse_auth_access_token(
                    token=token, secret=settings.auth_token_secret
                )
            except ValueError as exc:
                raise _admin_unauthorized(str(exc)) from exc
            return load_tenant_user_principal(session=session, user_id=user_id)
        set_platform_admin_rls_context(session)
        return _build_platform_admin_principal(username)

    if basic_credentials is not None:
        set_platform_admin_rls_context(session)
        if validate_admin_credentials(
            session=session,
            username=basic_credentials.username,
            password=basic_credentials.password,
        ):
            return _build_platform_admin_principal(basic_credentials.username)
        raise _admin_unauthorized()

    raise _admin_unauthorized("Authentication required")


def require_admin(
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
) -> str:
    if not principal.is_platform_super_admin or principal.username is None:
        raise _admin_unauthorized("Admin authentication required")
    return principal.username


def require_deployment_host_agent(
    bearer_credentials: HTTPAuthorizationCredentials | None = Depends(bearer_auth),
    session: Session = Depends(get_session),
) -> DeploymentHostPrincipal:
    if bearer_credentials is None or bearer_credentials.scheme.lower() != "bearer":
        raise _admin_unauthorized("Deployment host authentication required")
    token_hash = hash_deployment_host_token(bearer_credentials.credentials)
    host = session.execute(
        select(DeploymentHost).where(DeploymentHost.access_token_hash == token_hash)
    ).scalar_one_or_none()
    if host is None:
        raise _admin_unauthorized("Invalid deployment host credentials")
    set_platform_system_rls_context(session, system_purpose="deployment_host_agent")
    return DeploymentHostPrincipal(host_id=host.host_id, label=host.label)


def require_tenant_permission(
    *,
    principal: AuthenticatedPrincipal,
    tenant_id: str,
    permission_key: str,
) -> TenantMembershipPrincipal | None:
    if principal.is_platform_super_admin:
        return None
    normalized_permission_key = normalize_permission_key(permission_key)
    if normalized_permission_key is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown tenant permission"
        )
    membership = principal.membership_for_tenant(tenant_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    if normalized_permission_key not in membership.permission_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient tenant permissions",
        )
    return membership


def require_any_tenant_permission(
    *,
    principal: AuthenticatedPrincipal,
    tenant_id: str,
    permission_keys: tuple[str, ...],
) -> TenantMembershipPrincipal | None:
    if principal.is_platform_super_admin:
        return None
    normalized_permission_keys = tuple(
        normalized
        for normalized in (
            normalize_permission_key(permission_key)
            for permission_key in permission_keys
        )
        if normalized
    )
    if not normalized_permission_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown tenant permissions"
        )
    membership = principal.membership_for_tenant(tenant_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    if not any(
        permission_key in membership.permission_keys
        for permission_key in normalized_permission_keys
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient tenant permissions",
        )
    return membership


def require_tenant_membership(
    *, principal: AuthenticatedPrincipal, tenant_id: str
) -> TenantMembershipPrincipal | None:
    if principal.is_platform_super_admin:
        return None
    membership = principal.membership_for_tenant(tenant_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )
    return membership


def require_tenant_workspace_access(
    *,
    principal: AuthenticatedPrincipal,
    tenant_id: str,
) -> TenantMembershipPrincipal | None:
    if principal.is_platform_super_admin:
        return None
    membership = require_tenant_membership(principal=principal, tenant_id=tenant_id)
    if membership is None:
        return None
    if membership.role not in VALID_ROLE_KEYS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient tenant permissions",
        )
    return membership
