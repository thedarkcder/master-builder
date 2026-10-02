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


class WorkerRuntimeState(Base):
    __tablename__ = "worker_runtime_states"

    service_instance_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    agent_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    worker_mode: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    capabilities_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    runtime_kinds_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    runtime_dependencies_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="starting", index=True
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class WorkerRuntimeAuthRequest(Base):
    __tablename__ = "worker_runtime_auth_requests"
    __table_args__ = (
        Index(
            "ix_worker_runtime_auth_requests_scope",
            "service_instance_id",
            "runtime_kind",
            "status",
        ),
        Index("ix_worker_runtime_auth_requests_requested_at", "requested_at"),
    )

    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    service_instance_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("worker_runtime_states.service_instance_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    runtime_kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True, default="pending"
    )
    remediation_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class KnowledgeJiraSyncProjectState(Base):
    __tablename__ = "knowledge_jira_sync_project_states"
    __table_args__ = (
        UniqueConstraint(
            "runtime_name",
            "tenant_id",
            "project_id",
            name="uq_knowledge_jira_sync_project_states_scope",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    runtime_name: Mapped[str] = mapped_column(
        String(64),
        ForeignKey(
            "knowledge_jira_sync_runtime_states.runtime_name", ondelete="CASCADE"
        ),
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
    last_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
