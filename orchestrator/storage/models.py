from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from orchestrator.storage.vector_type import VectorJSONCompat


class Base(DeclarativeBase):
    pass


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


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "github_repository", name="uq_projects_tenant_repo"),
        UniqueConstraint("tenant_id", "jira_project_key", name="uq_projects_tenant_jira_key"),
    )

    project_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    github_repository: Mapped[str] = mapped_column(String(512), nullable=False)
    jira_project_key: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_overrides: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    environment: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    secret_refs: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    discord_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectAutomation(Base):
    __tablename__ = "project_automations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "kind", name="uq_project_automations_scope_kind"),
        Index("ix_project_automations_due_scan", "enabled", "next_run_at"),
    )

    automation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    days_of_week: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    local_time: Mapped[str] = mapped_column(String(8), nullable=False)
    delivery_text_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fallback_lookback_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    last_successful_window_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectAutomationExecution(Base):
    __tablename__ = "project_automation_executions"
    __table_args__ = (
        UniqueConstraint("automation_id", "scheduled_for", name="uq_project_automation_executions_automation_scheduled_for"),
        UniqueConstraint("dedupe_key", name="uq_project_automation_executions_dedupe_key"),
        Index("ix_project_automation_executions_due_scan", "status", "scheduled_for"),
        Index("ix_project_automation_executions_automation_history", "automation_id", "scheduled_for"),
    )

    execution_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    automation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("project_automations.automation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discord_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JiraOAuthConnection(Base):
    __tablename__ = "jira_oauth_connections"

    connection_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cloud_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    site_url: Mapped[str] = mapped_column(String(512), nullable=False)
    scopes: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    access_token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ManagedSecret(Base):
    __tablename__ = "managed_secrets"

    secret_ref: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PlatformSetting(Base):
    __tablename__ = "platform_settings"

    setting_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Run(Base):
    __tablename__ = "runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    dev_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pm_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    orchestrated_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    dedupe_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="issue_execution", index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    worker_service_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RunHumanInputRequest(Base):
    __tablename__ = "run_human_input_requests"

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    resumed_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    resume_stage: Mapped[str] = mapped_column(String(32), nullable=False)
    resume_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_reply_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    thread_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    thread_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PMInterviewCase(Base):
    __tablename__ = "pm_interview_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "request_id", name="uq_pm_interview_cases_tenant_request"),
        Index("ix_pm_interview_cases_tenant_status_channel", "tenant_id", "status", "channel_id"),
        Index("ix_pm_interview_cases_tenant_status_thread", "tenant_id", "status", "thread_channel_id"),
        Index("ix_pm_interview_cases_tenant_status_root_message", "tenant_id", "status", "root_message_id"),
        Index("ix_pm_interview_cases_tenant_status_owner", "tenant_id", "status", "owner_user_id"),
        Index("ix_pm_interview_cases_tenant_parent_issue", "tenant_id", "parent_issue_key"),
    )

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    parent_issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="drafting", index=True)
    channel_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    thread_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    root_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    brief_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    evidence_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    question_history_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    current_question_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    next_question_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    missing_slots_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    notes_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class FollowupContext(Base):
    __tablename__ = "followup_contexts"
    __table_args__ = (
        Index("ix_followup_contexts_tenant_status_thread", "tenant_id", "status", "thread_channel_id"),
        Index("ix_followup_contexts_tenant_status_channel", "tenant_id", "status", "channel_id"),
        Index("ix_followup_contexts_tenant_status_root_message", "tenant_id", "status", "root_message_id"),
        Index("ix_followup_contexts_tenant_status_request", "tenant_id", "status", "request_id"),
        Index("ix_followup_contexts_tenant_status_owner", "tenant_id", "status", "owner_user_id"),
        Index("ix_followup_contexts_tenant_type_issue", "tenant_id", "context_type", "issue_key"),
    )

    context_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    context_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    thread_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    root_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    owner_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    origin_command: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class RunLock(Base):
    __tablename__ = "run_locks"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    dedupe_scope: Mapped[str] = mapped_column(String(32), primary_key=True, default="issue_execution")
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    locked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TenantRunClaim(Base):
    __tablename__ = "tenant_run_claims"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    delivery_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WebhookJob(Base):
    __tablename__ = "webhook_jobs"
    __table_args__ = (
        UniqueConstraint("transport", "tenant_id", "dedupe_key", name="uq_webhook_jobs_transport_tenant_dedupe"),
        Index("ix_webhook_jobs_status_available_created", "status", "available_at", "created_at"),
        Index("ix_webhook_jobs_transport_subject_status_created", "transport", "subject_key", "status", "created_at"),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    transport: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subject_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    context_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class WebhookSubjectClaim(Base):
    __tablename__ = "webhook_subject_claims"

    subject_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PrReviewPublication(Base):
    __tablename__ = "pr_review_publications"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "repo_full_name",
            "pr_number",
            "head_sha",
            "review_kind",
            "signature",
            name="uq_pr_review_publications_scope",
        ),
    )

    publication_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    repo_full_name: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    pr_number: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    head_sha: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    review_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    signature: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    owner_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    review_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RepoBootstrapState(Base):
    __tablename__ = "repo_bootstrap_states"

    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    repo_url: Mapped[str] = mapped_column(String(512), primary_key=True)
    last_branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_created_files: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    bootstrap_count: Mapped[int] = mapped_column(nullable=False, default=1)
    bootstrapped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AgentLifecycleEvent(Base):
    __tablename__ = "agent_lifecycle_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class RunLogEvent(Base):
    __tablename__ = "run_log_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    invocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    command: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    working_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    attempt: Mapped[int | None] = mapped_column(nullable=True)
    stream: Mapped[str] = mapped_column(String(16), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class RunStreamEvent(Base):
    __tablename__ = "run_stream_events"
    __table_args__ = (
        Index("ix_run_stream_events_run_id_stream_offset", "run_id", "stream_offset"),
        Index("ix_run_stream_events_tenant_id_stream_offset", "tenant_id", "stream_offset"),
    )

    stream_offset: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    invocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    channel: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    command: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    working_dir: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    attempt: Mapped[int | None] = mapped_column(nullable=True)
    stream: Mapped[str | None] = mapped_column(String(16), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class RunTokenUsage(Base):
    __tablename__ = "run_token_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    attempt: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    invocation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cached_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delta_input: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delta_uncached: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delta_output: Mapped[int | None] = mapped_column(Integer, nullable=True)
    runtime_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    command: Mapped[str | None] = mapped_column(String(256), nullable=True, index=True)
    artifact_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    output_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    truncated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class KnowledgeAsset(Base):
    __tablename__ = "knowledge_assets"

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    binary_content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("asset_id", "chunk_index", name="uq_knowledge_chunks_asset_chunk_index"),
    )

    chunk_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("knowledge_assets.asset_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(VectorJSONCompat(384), nullable=True)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeFact(Base):
    __tablename__ = "knowledge_facts"

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("knowledge_assets.asset_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("knowledge_chunks.chunk_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fact_type: Mapped[str] = mapped_column(String(64), nullable=False, default="decision_slot", index=True)
    fact_key: Mapped[str] = mapped_column(String(128), nullable=False, default="", index=True)
    fact_value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    approval_state: Mapped[str] = mapped_column(String(32), nullable=False, default="approved", index=True)
    slot_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    slot_value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    is_inferred: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"

    source_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    connector_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active", index=True)
    sync_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="manual", index=True)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class KnowledgeJiraSyncRuntimeState(Base):
    __tablename__ = "knowledge_jira_sync_runtime_states"

    runtime_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="not_started")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    database_backend: Mapped[str] = mapped_column(String(32), nullable=False, default="other")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pass_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_pass_finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    leader_acquired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    service_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DiscordCommandSyncRuntimeState(Base):
    __tablename__ = "discord_command_sync_runtime_states"

    runtime_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    synced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    healthy: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    interaction_ingress_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    bot_token_configured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    guild_id_configured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    guild_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    application_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    command_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    service_instance_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class WorkerRuntimeState(Base):
    __tablename__ = "worker_runtime_states"

    service_instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    worker_mode: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    capabilities_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="starting", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class KnowledgeJiraSyncProjectState(Base):
    __tablename__ = "knowledge_jira_sync_project_states"
    __table_args__ = (
        UniqueConstraint("runtime_name", "tenant_id", "project_id", name="uq_knowledge_jira_sync_project_states_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runtime_name: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("knowledge_jira_sync_runtime_states.runtime_name", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    jira_project_key: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    failure_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DecisionCase(Base):
    __tablename__ = "decision_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "issue_key", name="uq_decision_cases_tenant_issue"),
    )

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(64), nullable=False, default="clear", index=True)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True)
    issue_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active_cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_source: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_event_type: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    required_worker_capability: Mapped[str | None] = mapped_column(String(32), nullable=True)
    required_worker_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    required_worker_label_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ready_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ready_label_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision_gate_closed_permanently: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decision_gate_closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    decision_gate_closed_cycle_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DecisionCycle(Base):
    __tablename__ = "decision_cycles"

    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    question_set_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    unresolved_question_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DecisionAnswer(Base):
    __tablename__ = "decision_answers"
    __table_args__ = (
        UniqueConstraint("cycle_id", "question_id", name="uq_decision_answers_cycle_question"),
    )

    answer_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cycle_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    question_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    question_kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    question_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    normalized_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_transport: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    evidence_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DecisionEvidence(Base):
    __tablename__ = "decision_evidence"
    __table_args__ = (
        UniqueConstraint("cycle_id", "dedupe_key", name="uq_decision_evidence_cycle_dedupe"),
    )

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cycle_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_transport: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    actor_ref: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    question_ids_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    normalized_answers_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DecisionEvent(Base):
    __tablename__ = "decision_events"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_decision_events_tenant_idempotency"),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cycle_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("decision_cycles.cycle_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    outcome_state: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)


class DecisionEffectOutbox(Base):
    __tablename__ = "decision_effects_outbox"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_decision_effects_tenant_dedupe"),
    )

    effect_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("decision_cases.case_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    cycle_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("decision_cycles.cycle_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    effect_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
