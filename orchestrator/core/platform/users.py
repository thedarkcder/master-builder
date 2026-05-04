from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median
from uuid import uuid4
import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform.passwords import hash_password, verify_password
from orchestrator.core.platform.access import (
    MODE_NON_TECHNICAL,
    MODE_TECHNICAL,
    PERMISSION_TECHNICAL_ACCESS,
    ROLE_BUSINESS_MEMBER,
    ROLE_TENANT_ADMIN,
    VALID_MODE_KEYS,
    VALID_ONBOARDING_KINDS,
    VALID_ROLE_KEYS,
)
from orchestrator.storage.models import (
    Run,
    Tenant,
    TenantInvite,
    TenantMembership,
    TenantTeam,
    TenantTeamMembership,
    TenantUser,
    TenantUserCredential,
    TenantUserDiscordIdentity,
)


INVITE_TTL_DAYS = 7


@dataclass(frozen=True)
class DeliverySummary:
    completed_count: int
    in_review_count: int
    blocked_count: int
    failed_count: int
    queued_count: int
    median_cycle_time_hours: float | None
    average_cycle_time_hours: float | None
    timeline: tuple[Run, ...]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def generate_id() -> str:
    return uuid4().hex


def normalize_email(email: str) -> str:
    return email.strip().lower()


def default_tenant_experience_config() -> dict[str, str]:
    return {"default_mode": MODE_TECHNICAL}


def default_tenant_setup_state() -> dict[str, object]:
    return {"onboarding_completed_at": None}


def default_tenant_configs() -> dict[str, object]:
    return {
        "jira_config": {
            "connection_id": None,
            "project_keys": [],
            "ready_statuses": ["Ready for Agent"],
            "ready_trigger_mode": "status_recheck",
            "ready_jql": None,
            "ready_label": "agent:ready",
            "in_progress_label": "agent:in-progress",
            "blocked_label": "agent:blocked",
            "done_label": "agent:done",
            "webhook_secret_ref": None,
            "managed_webhook_ids": [],
        },
        "github_config": {
            "webhook_secret_ref": None,
            "installation_id": None,
        },
        "repos_config": {
            "github_repository": None,
            "allowlist": [],
            "mapping_rules_by_project_key": {},
            "mapping_rules_by_component": {},
        },
        "policy_config": {
            "allow_jira_transitions": False,
            "allow_pr_creation": True,
            "allow_code_reviews": True,
            "allow_pr_remediation": True,
            "allow_manual_pr_fix_requests": True,
            "allow_label_mutations": True,
            "allow_auto_merge": False,
            "max_dev_test_review_loops": 2,
            "max_pr_auto_remediation_loops": 5,
            "max_concurrent_runs": 2,
            "allowed_commands": [],
            "require_agents_md": False,
            "knowledge_base_enabled": True,
            "knowledge_auto_answer_mode": "aggressive",
            "codex_model": None,
            "codex_reasoning_effort": None,
            "observability": {
                "audit_retention_days": 365,
                "audit_export_enabled": True,
                "legal_hold_enabled": False,
                "legal_hold_reason": None,
            },
        },
        "discord_config": None,
    }


def create_tenant_user(
    *,
    session: Session,
    email: str,
    full_name: str,
    password: str,
) -> TenantUser:
    normalized_email = normalize_email(email)
    now = utcnow()
    tenant_user = TenantUser(
        user_id=generate_id(),
        email=normalized_email,
        full_name=full_name.strip(),
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    session.add(tenant_user)
    session.add(
        TenantUserCredential(
            user_id=tenant_user.user_id,
            password_hash=hash_password(password),
            password_updated_at=now,
            must_change_password=False,
            created_at=now,
            updated_at=now,
        )
    )
    # Postgres enforces FKs during the same transaction. Flush the new user row before
    # any caller creates memberships or invite references that point at this user.
    session.flush()
    return tenant_user


def authenticate_tenant_user(*, session: Session, email: str, password: str) -> TenantUser | None:
    normalized_email = normalize_email(email)
    tenant_user = find_tenant_user_by_email(session=session, email=normalized_email)
    if tenant_user is None or not tenant_user.is_active:
        return None
    credential = session.get(TenantUserCredential, tenant_user.user_id)
    if credential is None:
        return None
    if not verify_password(password=password, password_hash=credential.password_hash):
        return None
    return tenant_user


def find_tenant_user_by_email(*, session: Session, email: str) -> TenantUser | None:
    normalized_email = normalize_email(email)
    return session.execute(select(TenantUser).where(TenantUser.email == normalized_email)).scalar_one_or_none()


def create_membership(
    *,
    session: Session,
    tenant_id: str,
    user_id: str,
    role: str,
    mode_override: str | None,
    onboarding_kind: str,
) -> TenantMembership:
    normalized_role = str(role).strip()
    if normalized_role not in VALID_ROLE_KEYS:
        raise ValueError("Invalid tenant role")
    normalized_mode_override = None if mode_override is None else str(mode_override).strip()
    if normalized_mode_override is not None and normalized_mode_override not in VALID_MODE_KEYS:
        raise ValueError("Invalid tenant mode override")
    if onboarding_kind not in VALID_ONBOARDING_KINDS:
        raise ValueError("Invalid onboarding kind")
    now = utcnow()
    membership = TenantMembership(
        membership_id=generate_id(),
        tenant_id=tenant_id,
        user_id=user_id,
        role=normalized_role,
        mode_override=normalized_mode_override,
        onboarding_kind=onboarding_kind,
        first_signed_in_at=None,
        onboarding_completed_at=None,
        onboarding_version="v1",
        created_at=now,
        updated_at=now,
    )
    session.add(membership)
    return membership


def add_membership_teams(*, session: Session, membership_id: str, team_ids: list[str]) -> None:
    now = utcnow()
    for team_id in team_ids:
        session.add(
            TenantTeamMembership(
                team_membership_id=generate_id(),
                team_id=team_id,
                membership_id=membership_id,
                created_at=now,
            )
        )


def mark_membership_signed_in(*, session: Session, user_id: str) -> None:
    now = utcnow()
    memberships = session.execute(
        select(TenantMembership).where(TenantMembership.user_id == user_id)
    ).scalars().all()
    changed = False
    for membership in memberships:
        if membership.first_signed_in_at is None:
            membership.first_signed_in_at = now
            membership.updated_at = now
            changed = True
    if changed:
        session.flush()


def create_invite(
    *,
    session: Session,
    tenant_id: str,
    email: str,
    full_name: str | None,
    role: str,
    team_ids: list[str],
    mode_override: str | None,
    invited_by_user_id: str | None,
) -> tuple[TenantInvite, str]:
    normalized_email = normalize_email(email)
    if role not in VALID_ROLE_KEYS:
        raise ValueError("Invalid tenant role")
    if mode_override is not None and mode_override not in VALID_MODE_KEYS:
        raise ValueError("Invalid tenant mode override")
    existing_pending = session.execute(
        select(TenantInvite).where(
            TenantInvite.tenant_id == tenant_id,
            TenantInvite.email == normalized_email,
            TenantInvite.status == "pending",
        )
    ).scalar_one_or_none()
    if existing_pending is not None:
        raise ValueError("A pending invite already exists for this email")
    now = utcnow()
    raw_token = secrets.token_urlsafe(32)
    invite = TenantInvite(
        invite_id=generate_id(),
        tenant_id=tenant_id,
        email=normalized_email,
        full_name=full_name.strip() if full_name else None,
        role=role,
        team_ids=list(team_ids),
        mode_override=mode_override,
        status="pending",
        invite_token_hash=hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
        invited_by_user_id=invited_by_user_id,
        accepted_by_user_id=None,
        expires_at=now + timedelta(days=INVITE_TTL_DAYS),
        accepted_at=None,
        revoked_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(invite)
    return invite, raw_token


def list_teams(*, session: Session, tenant_id: str) -> list[TenantTeam]:
    return session.execute(
        select(TenantTeam).where(TenantTeam.tenant_id == tenant_id).order_by(TenantTeam.name.asc(), TenantTeam.team_id.asc())
    ).scalars().all()


def get_team(*, session: Session, tenant_id: str, team_id: str) -> TenantTeam | None:
    return session.execute(
        select(TenantTeam).where(TenantTeam.tenant_id == tenant_id, TenantTeam.team_id == team_id)
    ).scalar_one_or_none()


def create_team(
    *,
    session: Session,
    tenant_id: str,
    name: str,
    description: str | None,
    permission_keys: list[str],
) -> TenantTeam:
    now = utcnow()
    team = TenantTeam(
        team_id=generate_id(),
        tenant_id=tenant_id,
        name=name.strip(),
        description=description.strip() if description else None,
        permission_keys=list(permission_keys),
        created_at=now,
        updated_at=now,
    )
    session.add(team)
    return team


def update_team(
    *,
    session: Session,
    tenant_id: str,
    team_id: str,
    name: str,
    description: str | None,
    permission_keys: list[str],
) -> TenantTeam:
    team = get_team(session=session, tenant_id=tenant_id, team_id=team_id)
    if team is None:
        raise ValueError("Team not found")
    team.name = name.strip()
    team.description = description.strip() if description else None
    team.permission_keys = list(permission_keys)
    team.updated_at = utcnow()
    return team


def list_invites(*, session: Session, tenant_id: str) -> list[TenantInvite]:
    return session.execute(
        select(TenantInvite)
        .where(TenantInvite.tenant_id == tenant_id)
        .order_by(TenantInvite.created_at.desc(), TenantInvite.invite_id.desc())
    ).scalars().all()


def get_invite(*, session: Session, tenant_id: str, invite_id: str) -> TenantInvite | None:
    return session.execute(
        select(TenantInvite).where(TenantInvite.tenant_id == tenant_id, TenantInvite.invite_id == invite_id)
    ).scalar_one_or_none()


def revoke_invite(*, session: Session, invite: TenantInvite) -> TenantInvite:
    now = utcnow()
    invite.status = "revoked"
    invite.revoked_at = now
    invite.updated_at = now
    return invite


def resend_invite(
    *,
    session: Session,
    invite: TenantInvite,
    invited_by_user_id: str | None,
) -> tuple[TenantInvite, str]:
    revoke_invite(session=session, invite=invite)
    session.flush()
    return create_invite(
        session=session,
        tenant_id=invite.tenant_id,
        email=invite.email,
        full_name=invite.full_name,
        role=invite.role,
        team_ids=list(invite.team_ids or []),
        mode_override=invite.mode_override,
        invited_by_user_id=invited_by_user_id,
    )


def resolve_invite(*, session: Session, raw_token: str) -> TenantInvite | None:
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    invite = session.execute(
        select(TenantInvite).where(TenantInvite.invite_token_hash == token_hash)
    ).scalar_one_or_none()
    if invite is None:
        return None
    now = utcnow()
    expires_at = _coerce_utc(invite.expires_at)
    if invite.status != "pending" or expires_at is None or expires_at <= now:
        return None
    return invite


def accept_invite(
    *,
    session: Session,
    invite: TenantInvite,
    password: str,
    full_name: str | None,
) -> TenantUser:
    normalized_email = normalize_email(invite.email)
    tenant_user = session.execute(
        select(TenantUser).where(TenantUser.email == normalized_email)
    ).scalar_one_or_none()
    now = utcnow()
    if tenant_user is None:
        tenant_user = create_tenant_user(
            session=session,
            email=normalized_email,
            full_name=full_name or invite.full_name or normalized_email,
            password=password,
        )
    else:
        credential = session.get(TenantUserCredential, tenant_user.user_id)
        if credential is None:
            session.add(
                TenantUserCredential(
                    user_id=tenant_user.user_id,
                    password_hash=hash_password(password),
                    password_updated_at=now,
                    must_change_password=False,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            credential.password_hash = hash_password(password)
            credential.password_updated_at = now
            credential.must_change_password = False
            credential.updated_at = now
        if full_name:
            tenant_user.full_name = full_name.strip()
            tenant_user.updated_at = now

    existing_membership = session.execute(
        select(TenantMembership).where(
            TenantMembership.tenant_id == invite.tenant_id,
            TenantMembership.user_id == tenant_user.user_id,
        )
    ).scalar_one_or_none()
    if existing_membership is None:
        membership = create_membership(
            session=session,
            tenant_id=invite.tenant_id,
            user_id=tenant_user.user_id,
            role=invite.role,
            mode_override=invite.mode_override,
            onboarding_kind="member_join",
        )
        add_membership_teams(session=session, membership_id=membership.membership_id, team_ids=list(invite.team_ids or []))
    else:
        existing_membership.role = invite.role
        existing_membership.mode_override = invite.mode_override
        existing_membership.updated_at = now
        existing_team_ids = {
            team_id
            for team_id, in session.execute(
                select(TenantTeamMembership.team_id).where(TenantTeamMembership.membership_id == existing_membership.membership_id)
            ).all()
        }
        missing_team_ids = [team_id for team_id in list(invite.team_ids or []) if team_id not in existing_team_ids]
        add_membership_teams(session=session, membership_id=existing_membership.membership_id, team_ids=missing_team_ids)

    # Persist the user row before recording it as the invite acceptor. Postgres enforces
    # the FK on tenant_invites.accepted_by_user_id immediately during the same flush.
    session.flush()

    invite.status = "accepted"
    invite.accepted_by_user_id = tenant_user.user_id
    invite.accepted_at = now
    invite.updated_at = now
    return tenant_user


def ensure_team_ids_exist(*, session: Session, tenant_id: str, team_ids: list[str]) -> None:
    if not team_ids:
        return
    existing_ids = {
        team_id
        for team_id, in session.execute(
            select(TenantTeam.team_id).where(TenantTeam.tenant_id == tenant_id, TenantTeam.team_id.in_(team_ids))
        ).all()
    }
    missing = sorted(set(team_ids) - existing_ids)
    if missing:
        raise ValueError(f"Unknown team ids: {', '.join(missing)}")


def list_memberships_with_users(*, session: Session, tenant_id: str) -> list[tuple[TenantMembership, TenantUser]]:
    return session.execute(
        select(TenantMembership, TenantUser)
        .join(TenantUser, TenantUser.user_id == TenantMembership.user_id)
        .where(TenantMembership.tenant_id == tenant_id)
        .order_by(TenantUser.full_name.asc(), TenantUser.email.asc(), TenantMembership.created_at.asc())
    ).all()


def replace_membership_teams(*, session: Session, membership_id: str, team_ids: list[str]) -> None:
    existing = session.execute(
        select(TenantTeamMembership).where(TenantTeamMembership.membership_id == membership_id)
    ).scalars().all()
    existing_by_team = {row.team_id: row for row in existing}
    desired = list(dict.fromkeys(team_ids))
    for team_id, row in existing_by_team.items():
        if team_id not in desired:
            session.delete(row)
    add_membership_teams(
        session=session,
        membership_id=membership_id,
        team_ids=[team_id for team_id in desired if team_id not in existing_by_team],
    )


def update_membership(
    *,
    session: Session,
    membership_id: str,
    role: str,
    mode_override: str | None,
    team_ids: list[str],
    is_active: bool,
) -> TenantMembership:
    membership = session.get(TenantMembership, membership_id)
    if membership is None:
        raise ValueError("Membership not found")
    membership.role = role
    membership.mode_override = mode_override
    membership.updated_at = utcnow()
    tenant_user = session.get(TenantUser, membership.user_id)
    if tenant_user is None:
        raise ValueError("User not found")
    tenant_user.is_active = is_active
    tenant_user.updated_at = membership.updated_at
    replace_membership_teams(session=session, membership_id=membership_id, team_ids=team_ids)
    return membership


def update_user_profile(
    *,
    session: Session,
    user_id: str,
    full_name: str,
) -> TenantUser:
    tenant_user = session.get(TenantUser, user_id)
    if tenant_user is None:
        raise ValueError("User not found")
    tenant_user.full_name = full_name.strip()
    tenant_user.updated_at = utcnow()
    return tenant_user


def update_membership_mode_override(
    *,
    session: Session,
    membership_id: str,
    role: str,
    permission_keys: list[str],
    mode_override: str | None,
) -> TenantMembership:
    membership = session.get(TenantMembership, membership_id)
    if membership is None:
        raise ValueError("Membership not found")
    if mode_override is not None and mode_override not in VALID_MODE_KEYS:
        raise ValueError("Invalid tenant mode override")
    normalized_mode_override = mode_override
    if mode_override == MODE_TECHNICAL and PERMISSION_TECHNICAL_ACCESS not in permission_keys and role == ROLE_BUSINESS_MEMBER:
        normalized_mode_override = None
    membership.mode_override = normalized_mode_override
    membership.updated_at = utcnow()
    return membership


def change_user_password(
    *,
    session: Session,
    user_id: str,
    current_password: str,
    new_password: str,
) -> TenantUserCredential:
    credential = session.get(TenantUserCredential, user_id)
    if credential is None:
        raise ValueError("Credential not found")
    if not verify_password(password=current_password, password_hash=credential.password_hash):
        raise PermissionError("Current password is incorrect")
    now = utcnow()
    credential.password_hash = hash_password(new_password)
    credential.password_updated_at = now
    credential.must_change_password = False
    credential.updated_at = now
    return credential


def reset_user_password(
    *,
    session: Session,
    user_id: str,
    new_password: str,
) -> TenantUserCredential:
    credential = session.get(TenantUserCredential, user_id)
    if credential is None:
        raise ValueError("Credential not found")
    now = utcnow()
    credential.password_hash = hash_password(new_password)
    credential.password_updated_at = now
    credential.must_change_password = False
    credential.updated_at = now
    return credential


def get_discord_identity(*, session: Session, user_id: str) -> TenantUserDiscordIdentity | None:
    return session.get(TenantUserDiscordIdentity, user_id)


def link_discord_identity(
    *,
    session: Session,
    user_id: str,
    discord_user_id: str,
    discord_username: str | None,
    discord_global_name: str | None,
    discord_avatar_hash: str | None,
) -> TenantUserDiscordIdentity:
    now = utcnow()
    identity = session.get(TenantUserDiscordIdentity, user_id)
    if identity is None:
        identity = TenantUserDiscordIdentity(
            user_id=user_id,
            discord_user_id=discord_user_id,
            discord_username=discord_username,
            discord_global_name=discord_global_name,
            discord_avatar_hash=discord_avatar_hash,
            linked_at=now,
            updated_at=now,
        )
        session.add(identity)
    else:
        identity.discord_user_id = discord_user_id
        identity.discord_username = discord_username
        identity.discord_global_name = discord_global_name
        identity.discord_avatar_hash = discord_avatar_hash
        identity.updated_at = now
    return identity


def update_membership_discord_state(
    *,
    session: Session,
    membership_id: str,
    mutate: Callable[[dict], None],
) -> TenantMembership:
    membership = session.get(TenantMembership, membership_id)
    if membership is None:
        raise ValueError("Membership not found")
    state = dict(membership.discord_state or {})
    mutate(state)
    membership.discord_state = state
    membership.updated_at = utcnow()
    return membership


def summarize_delivery(*, session: Session, tenant_id: str) -> DeliverySummary:
    runs = session.execute(
        select(Run)
        .where(Run.tenant_id == tenant_id)
        .order_by(Run.finished_at.desc().nullslast(), Run.created_at.desc())
    ).scalars().all()
    completed_count = 0
    in_review_count = 0
    blocked_count = 0
    failed_count = 0
    queued_count = 0
    cycle_times: list[float] = []

    for run in runs:
        normalized_status = str(run.status or "").strip().lower()
        if normalized_status == "succeeded":
            completed_count += 1
        elif normalized_status == "blocked":
            blocked_count += 1
        elif normalized_status == "failed":
            failed_count += 1
        elif normalized_status in {"review", "in_review"}:
            in_review_count += 1
        else:
            queued_count += 1
        if run.started_at is not None and run.finished_at is not None:
            cycle_times.append(max(0.0, (run.finished_at - run.started_at).total_seconds() / 3600))

    average_cycle_time = round(sum(cycle_times) / len(cycle_times), 2) if cycle_times else None
    median_cycle_time = round(float(median(cycle_times)), 2) if cycle_times else None
    return DeliverySummary(
        completed_count=completed_count,
        in_review_count=in_review_count,
        blocked_count=blocked_count,
        failed_count=failed_count,
        queued_count=queued_count,
        median_cycle_time_hours=median_cycle_time,
        average_cycle_time_hours=average_cycle_time,
        timeline=tuple(runs[:50]),
    )


def can_use_technical_mode(*, role: str, mode_override: str | None) -> str | None:
    if mode_override is None:
        return None
    if mode_override == MODE_NON_TECHNICAL:
        return mode_override
    if role == ROLE_TENANT_ADMIN:
        return MODE_TECHNICAL
    return mode_override


def create_tenant_record(*, tenant_id: str, tenant_name: str) -> Tenant:
    now = utcnow()
    defaults = default_tenant_configs()
    return Tenant(
        tenant_id=tenant_id,
        name=tenant_name.strip(),
        is_enabled=True,
        jira_config=defaults["jira_config"],
        github_config=defaults["github_config"],
        repos_config=defaults["repos_config"],
        policy_config=defaults["policy_config"],
        discord_config=defaults["discord_config"],
        experience_config=default_tenant_experience_config(),
        setup_state=default_tenant_setup_state(),
        created_at=now,
        updated_at=now,
    )
