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


class WorkflowOperation(Base):
    __tablename__ = "workflow_operations"
    __table_args__ = (
        Index("ix_workflow_operations_workflow_id", "workflow_id"),
        Index("ix_workflow_operations_run_id", "run_id"),
        Index("ix_workflow_operations_status", "status"),
        UniqueConstraint("workflow_id", "idempotency_key", name="uq_workflow_operations_idempotency"),
    )

    operation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
        nullable=False,
    )
    run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("runs.run_id", ondelete="SET NULL"),
        nullable=True,
    )
    operation_type: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    target_system: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkflowOperationAttempt(Base):
    __tablename__ = "workflow_operation_attempts"
    __table_args__ = (
        Index("ix_workflow_operation_attempts_operation_id", "operation_id"),
        Index("ix_workflow_operation_attempts_status", "status"),
        Index("ix_workflow_operation_attempts_last_heartbeat_at", "last_heartbeat_at"),
        Index("ix_workflow_operation_attempts_lease_expires_at", "lease_expires_at"),
        Index(
            "uq_workflow_operation_active_attempt",
            "operation_id",
            unique=True,
            postgresql_where=text("status IN ('running', 'waiting_for_input')"),
            sqlite_where=text("status IN ('running', 'waiting_for_input')"),
        ),
        UniqueConstraint("operation_id", "attempt_number", name="uq_workflow_operation_attempt_number"),
        UniqueConstraint("operation_id", "attempt_id", name="uq_workflow_operation_attempt_identity"),
    )

    attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_operations.operation_id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowOperationWorkUnit(Base):
    __tablename__ = "workflow_operation_work_units"
    __table_args__ = (
        Index("ix_workflow_operation_work_units_operation_id", "operation_id"),
        Index("ix_workflow_operation_work_units_parent_attempt_id", "parent_attempt_id"),
        Index("ix_workflow_operation_work_units_unit_key", "unit_key"),
        Index("ix_workflow_operation_work_units_status", "status"),
        UniqueConstraint(
            "operation_id",
            "unit_key",
            "idempotency_key",
            "input_fingerprint",
            name="uq_workflow_operation_work_unit_identity",
        ),
    )

    work_unit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_operations.operation_id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_attempt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_operation_attempts.attempt_id", ondelete="CASCADE"),
        nullable=False,
    )
    unit_key: Mapped[str] = mapped_column(String(128), nullable=False)
    unit_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WorkflowOperationWorkUnitAttempt(Base):
    __tablename__ = "workflow_operation_work_unit_attempts"
    __table_args__ = (
        Index("ix_workflow_operation_work_unit_attempts_work_unit_id", "work_unit_id"),
        Index("ix_workflow_operation_work_unit_attempts_operation_attempt_id", "operation_attempt_id"),
        Index("ix_workflow_operation_work_unit_attempts_status", "status"),
        UniqueConstraint("work_unit_id", "attempt_number", name="uq_workflow_operation_work_unit_attempt_number"),
    )

    work_unit_attempt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    work_unit_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_operation_work_units.work_unit_id", ondelete="CASCADE"),
        nullable=False,
    )
    operation_attempt_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("workflow_operation_attempts.attempt_id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    error_category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
