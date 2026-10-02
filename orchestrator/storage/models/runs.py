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


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_workflow_id", "workflow_id"),
        Index("ix_runs_workflow_attempt", "workflow_id", "attempt_number", unique=True),
        Index("ix_runs_pre_check_outcome", "pre_check_outcome"),
        Index("ix_runs_required_worker_capability", "required_worker_capability"),
        Index("ix_runs_claim_id", "claim_id"),
        Index("ix_runs_dispatch_claimed_at", "dispatch_claimed_at"),
    )

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
        nullable=False,
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
    issue_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    issue_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    entry_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="fresh")
    entry_stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    entry_checkpoint_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    dedupe_scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default="issue_execution", index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pre_check_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    required_worker_capability: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    required_runtime_kinds_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list
    )
    claim_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    dispatch_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    worker_service_instance_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RunHumanInputRequest(Base):
    __tablename__ = "run_human_input_requests"
    __table_args__ = (
        Index(
            "uq_run_human_input_requests_pending_workflow",
            "workflow_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

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
    workflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_checkpoints.checkpoint_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    consumed_by_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_stage: Mapped[str] = mapped_column(String(64), nullable=False)
    request_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    expected_reply_format: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    request_context_json: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    thread_channel_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    thread_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    answer_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    answer_source_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
