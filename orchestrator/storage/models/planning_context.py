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


class PlanningDecisionRecord(Base):
    __tablename__ = "planning_decision_records"
    __table_args__ = (
        Index("ix_planning_decision_records_tenant_workflow", "tenant_id", "workflow_id"),
        Index("ix_planning_decision_records_tenant_issue", "tenant_id", "parent_issue_key"),
        Index("ix_planning_decision_records_tenant_lane_status", "tenant_id", "lane", "status"),
        Index("ix_planning_decision_records_source_attempt", "source_attempt_id"),
        Index(
            "uq_planning_decision_records_identity",
            "tenant_id",
            "workflow_id",
            "lane",
            "source_stage",
            "external_key",
            unique=True,
        ),
    )

    record_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
        String(128),
        ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    source_operation_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("workflow_operations.operation_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_attempt_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("workflow_operation_attempts.attempt_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    parent_issue_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    lane: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source_stage: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    external_key: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
