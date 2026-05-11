from __future__ import annotations

# Model modules share SQLAlchemy symbols from the storage model base.
# ruff: noqa: F401
from .base import (
    Base,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Mapped,
    String,
    Text,
    UniqueConstraint,
    datetime,
    mapped_column,
)


class ProjectApp(Base):
    __tablename__ = "project_apps"
    __table_args__ = (
        UniqueConstraint("project_id", "source_path", name="uq_project_apps_project_source_path"),
        UniqueConstraint("project_id", "slug", name="uq_project_apps_project_slug"),
        Index("ix_project_apps_tenant_project_created_at", "tenant_id", "project_id", "created_at"),
        Index("ix_project_apps_project_status", "project_id", "status"),
        Index("ix_project_apps_tenant_status", "tenant_id", "status"),
    )

    app_id: Mapped[str] = mapped_column(String(128), primary_key=True)
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
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False)
    detection_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    detected_runtime: Mapped[str | None] = mapped_column(String(128), nullable=True)
    detected_language: Mapped[str | None] = mapped_column(String(128), nullable=True)
    analysis_source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    build_strategy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    exposed_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    healthcheck: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_command: Mapped[str | None] = mapped_column(Text, nullable=True)
    env_schema_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    secret_schema_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deployment_config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectAppAnalysisRun(Base):
    __tablename__ = "project_app_analysis_runs"
    __table_args__ = (
        Index("ix_project_app_analysis_runs_tenant_project_created_at", "tenant_id", "project_id", "created_at"),
        Index("ix_project_app_analysis_runs_project_status", "project_id", "status"),
        Index("ix_project_app_analysis_runs_tenant_status", "tenant_id", "status"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    planner_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectDeploymentRelease(Base):
    __tablename__ = "project_deployment_releases"
    __table_args__ = (
        Index("ix_project_deployment_releases_tenant_project_created_at", "tenant_id", "project_id", "created_at"),
        Index("ix_project_deployment_releases_project_status", "project_id", "status"),
    )

    release_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    app_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("project_apps.app_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    environment_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    git_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    provider_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ProjectDeploymentRestoreRun(Base):
    __tablename__ = "project_deployment_restore_runs"
    __table_args__ = (
        Index("ix_project_deployment_restore_runs_tenant_project_created_at", "tenant_id", "project_id", "created_at"),
        Index("ix_project_deployment_restore_runs_app_status", "app_id", "status"),
        Index("ix_project_deployment_restore_runs_tenant_status", "tenant_id", "status"),
    )

    restore_run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    app_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("project_apps.app_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    host_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("deployment_hosts.host_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    command_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("deployment_host_commands.command_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    backup_policy_key: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_key: Mapped[str] = mapped_column(String(255), nullable=False)
    backup_uuid: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_uuid: Mapped[str] = mapped_column(String(128), nullable=False)
    database_type: Mapped[str] = mapped_column(String(32), nullable=False)
    database_uuid: Mapped[str] = mapped_column(String(128), nullable=False)
    restore_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="replace")
    requested_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmation_value: Mapped[str] = mapped_column(String(255), nullable=False)
    execution_payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeploymentHost(Base):
    __tablename__ = "deployment_hosts"
    __table_args__ = (
        Index("ix_deployment_hosts_state_last_seen_at", "state", "last_seen_at"),
        Index("ix_deployment_hosts_provider_region", "provider", "region"),
    )

    host_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    infrastructure_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    region: Mapped[str | None] = mapped_column(String(128), nullable=True)
    capability_keys_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    agent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="provisioning", index=True)
    bootstrap_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    access_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    registered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DeploymentHostCommand(Base):
    __tablename__ = "deployment_host_commands"
    __table_args__ = (
        Index("ix_deployment_host_commands_host_status_available_at", "host_id", "status", "available_at"),
        Index("ix_deployment_host_commands_restore_run_id", "restore_run_id"),
        Index("ix_deployment_host_commands_tenant_project_created_at", "tenant_id", "project_id", "created_at"),
    )

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    host_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("deployment_hosts.host_id", ondelete="CASCADE"),
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
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    app_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("project_apps.app_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    restore_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("project_deployment_restore_runs.restore_run_id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued", index=True)
    claim_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    result_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
