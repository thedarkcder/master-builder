from __future__ import annotations

# Model modules share SQLAlchemy symbols from the storage model base.
# ruff: noqa: F401
from .base import (
    Base,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    Mapped,
    String,
    Text,
    UniqueConstraint,
    VectorJSONCompat,
    datetime,
    mapped_column,
    text,
    uuid4,
)


class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    purge_after_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    jira_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    github_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    repos_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    policy_config: Mapped[dict] = mapped_column(JSON, nullable=False)
    discord_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    experience_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    setup_state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deployment_plane_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantUser(Base):
    __tablename__ = "tenant_users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantUserCredential(Base):
    __tablename__ = "tenant_user_credentials"

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant_users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    password_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantMembership(Base):
    __tablename__ = "tenant_memberships"
    __table_args__ = (UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),)

    membership_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant_users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    mode_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    onboarding_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="member_join")
    first_signed_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    discord_state: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantTeam(Base):
    __tablename__ = "tenant_teams"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tenant_teams_tenant_name"),)

    team_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    permission_keys: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantTeamMembership(Base):
    __tablename__ = "tenant_team_memberships"
    __table_args__ = (UniqueConstraint("team_id", "membership_id", name="uq_tenant_team_memberships"),)

    team_membership_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    team_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant_teams.team_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    membership_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant_memberships.membership_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantInvite(Base):
    __tablename__ = "tenant_invites"
    __table_args__ = (
        Index("ix_tenant_invites_tenant_status_created", "tenant_id", "status", "created_at"),
    )

    invite_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    team_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    mode_override: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    invite_token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    invited_by_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tenant_users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    accepted_by_user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tenant_users.user_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantUserDiscordIdentity(Base):
    __tablename__ = "tenant_user_discord_identities"

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tenant_users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    discord_user_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    discord_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discord_global_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discord_avatar_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    linked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
