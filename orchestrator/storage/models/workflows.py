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


class WorkflowExecution(Base):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        Index("ix_workflow_executions_tenant_id", "tenant_id"),
        Index("ix_workflow_executions_project_id", "project_id"),
        Index("ix_workflow_executions_source", "source_system", "source_ref"),
        Index("ix_workflow_executions_status", "status"),
        CheckConstraint(
            "orchestration_backend IN ('legacy', 'temporal', 'database')",
            name="ck_workflow_executions_orchestration_backend",
        ),
        Index(
            "uq_workflow_executions_active_scope",
            "tenant_id",
            "source_system",
            "source_ref",
            "dedupe_scope",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'waiting_for_input')"),
            sqlite_where=text("status IN ('queued', 'running', 'waiting_for_input')"),
        ),
    )

    workflow_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    execution_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        default=lambda: uuid4().hex,
    )
    workflow_type_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("projects.project_id", ondelete="SET NULL"),
        nullable=True,
    )
    source_system: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(255), nullable=False)
    source_external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    orchestration_backend: Mapped[str] = mapped_column(String(32), nullable=False)
    dedupe_scope: Mapped[str] = mapped_column(String(32), nullable=False, default="issue_execution")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    active_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_checkpoint_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_workflow_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("workflow_executions.workflow_id", ondelete="SET NULL"),
        nullable=True,
    )
    source_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowCheckpoint(Base):
    __tablename__ = "workflow_checkpoints"
    __table_args__ = (
        Index("ix_workflow_checkpoints_workflow_id", "workflow_id"),
        Index("ix_workflow_checkpoints_run_id", "run_id"),
        Index("ix_workflow_checkpoints_kind", "checkpoint_kind"),
        Index("ix_workflow_checkpoints_created_at", "created_at"),
        UniqueConstraint("run_id", "checkpoint_kind", name="uq_workflow_checkpoints_run_kind"),
    )

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    checkpoint_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    codex_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
