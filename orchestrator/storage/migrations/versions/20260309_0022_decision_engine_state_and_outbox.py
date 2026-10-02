"""decision engine state and outbox tables

Revision ID: 20260309_0022
Revises: 20260309_0021
Create Date: 2026-03-09
"""

from alembic import op
import sqlalchemy as sa


revision = "20260309_0022"
down_revision = "20260309_0021"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def _create_index_if_missing(
    index_name: str, table_name: str, columns: list[str], *, unique: bool = False
) -> None:
    if _table_exists(table_name) and not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, columns, unique=unique)


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    if _table_exists(table_name) and _index_exists(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)


def upgrade() -> None:
    if not _table_exists("decision_cases"):
        op.create_table(
            "decision_cases",
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=64), nullable=False),
            sa.Column("blocked_reason", sa.Text(), nullable=True),
            sa.Column("classification", sa.String(length=32), nullable=True),
            sa.Column("issue_fingerprint", sa.String(length=64), nullable=True),
            sa.Column("active_cycle_id", sa.String(length=64), nullable=True),
            sa.Column("last_source", sa.String(length=64), nullable=True),
            sa.Column("last_event_type", sa.String(length=64), nullable=True),
            sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "required_worker_capability", sa.String(length=32), nullable=True
            ),
            sa.Column("required_worker_label", sa.String(length=64), nullable=True),
            sa.Column("ready_label", sa.String(length=64), nullable=True),
            sa.Column(
                "ready_label_present",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("case_id"),
            sa.UniqueConstraint(
                "tenant_id", "issue_key", name="uq_decision_cases_tenant_issue"
            ),
        )
    _create_index_if_missing(
        "ix_decision_cases_tenant_id", "decision_cases", ["tenant_id"]
    )
    _create_index_if_missing(
        "ix_decision_cases_project_id", "decision_cases", ["project_id"]
    )
    _create_index_if_missing(
        "ix_decision_cases_issue_key", "decision_cases", ["issue_key"]
    )
    _create_index_if_missing("ix_decision_cases_state", "decision_cases", ["state"])
    _create_index_if_missing(
        "ix_decision_cases_active_cycle_id", "decision_cases", ["active_cycle_id"]
    )
    _create_index_if_missing(
        "ix_decision_cases_last_source", "decision_cases", ["last_source"]
    )
    _create_index_if_missing(
        "ix_decision_cases_last_event_type", "decision_cases", ["last_event_type"]
    )
    _create_index_if_missing(
        "ix_decision_cases_last_event_at", "decision_cases", ["last_event_at"]
    )
    _create_index_if_missing(
        "ix_decision_cases_updated_at", "decision_cases", ["updated_at"]
    )

    if not _table_exists("decision_cycles"):
        op.create_table(
            "decision_cycles",
            sa.Column("cycle_id", sa.String(length=64), nullable=False),
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("classification", sa.String(length=32), nullable=True),
            sa.Column("question_set_json", sa.JSON(), nullable=False),
            sa.Column("unresolved_question_ids_json", sa.JSON(), nullable=False),
            sa.Column("metadata_json", sa.JSON(), nullable=False),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["case_id"], ["decision_cases.case_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("cycle_id"),
        )
    _create_index_if_missing(
        "ix_decision_cycles_case_id", "decision_cycles", ["case_id"]
    )
    _create_index_if_missing(
        "ix_decision_cycles_tenant_id", "decision_cycles", ["tenant_id"]
    )
    _create_index_if_missing(
        "ix_decision_cycles_project_id", "decision_cycles", ["project_id"]
    )
    _create_index_if_missing(
        "ix_decision_cycles_issue_key", "decision_cycles", ["issue_key"]
    )
    _create_index_if_missing("ix_decision_cycles_status", "decision_cycles", ["status"])
    _create_index_if_missing(
        "ix_decision_cycles_classification", "decision_cycles", ["classification"]
    )
    _create_index_if_missing(
        "ix_decision_cycles_opened_at", "decision_cycles", ["opened_at"]
    )
    _create_index_if_missing(
        "ix_decision_cycles_closed_at", "decision_cycles", ["closed_at"]
    )
    _create_index_if_missing(
        "ix_decision_cycles_updated_at", "decision_cycles", ["updated_at"]
    )

    if not _table_exists("decision_events"):
        op.create_table(
            "decision_events",
            sa.Column("event_id", sa.String(length=64), nullable=False),
            sa.Column("idempotency_key", sa.String(length=255), nullable=False),
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("cycle_id", sa.String(length=64), nullable=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("source", sa.String(length=64), nullable=False),
            sa.Column("event_type", sa.String(length=64), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("outcome_state", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["case_id"], ["decision_cases.case_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["cycle_id"], ["decision_cycles.cycle_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("event_id"),
            sa.UniqueConstraint(
                "tenant_id",
                "idempotency_key",
                name="uq_decision_events_tenant_idempotency",
            ),
        )
    _create_index_if_missing(
        "ix_decision_events_idempotency_key", "decision_events", ["idempotency_key"]
    )
    _create_index_if_missing(
        "ix_decision_events_case_id", "decision_events", ["case_id"]
    )
    _create_index_if_missing(
        "ix_decision_events_cycle_id", "decision_events", ["cycle_id"]
    )
    _create_index_if_missing(
        "ix_decision_events_tenant_id", "decision_events", ["tenant_id"]
    )
    _create_index_if_missing(
        "ix_decision_events_project_id", "decision_events", ["project_id"]
    )
    _create_index_if_missing(
        "ix_decision_events_issue_key", "decision_events", ["issue_key"]
    )
    _create_index_if_missing("ix_decision_events_source", "decision_events", ["source"])
    _create_index_if_missing(
        "ix_decision_events_event_type", "decision_events", ["event_type"]
    )
    _create_index_if_missing(
        "ix_decision_events_outcome_state", "decision_events", ["outcome_state"]
    )
    _create_index_if_missing(
        "ix_decision_events_created_at", "decision_events", ["created_at"]
    )

    if not _table_exists("decision_effects_outbox"):
        op.create_table(
            "decision_effects_outbox",
            sa.Column("effect_id", sa.String(length=64), nullable=False),
            sa.Column("dedupe_key", sa.String(length=255), nullable=False),
            sa.Column("case_id", sa.String(length=64), nullable=False),
            sa.Column("cycle_id", sa.String(length=64), nullable=True),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("effect_type", sa.String(length=64), nullable=False),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column(
                "attempt_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["case_id"], ["decision_cases.case_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["cycle_id"], ["decision_cycles.cycle_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="SET NULL"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("effect_id"),
            sa.UniqueConstraint(
                "tenant_id", "dedupe_key", name="uq_decision_effects_tenant_dedupe"
            ),
        )
    _create_index_if_missing(
        "ix_decision_effects_outbox_dedupe_key",
        "decision_effects_outbox",
        ["dedupe_key"],
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_case_id", "decision_effects_outbox", ["case_id"]
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_cycle_id", "decision_effects_outbox", ["cycle_id"]
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_tenant_id", "decision_effects_outbox", ["tenant_id"]
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_project_id",
        "decision_effects_outbox",
        ["project_id"],
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_issue_key", "decision_effects_outbox", ["issue_key"]
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_effect_type",
        "decision_effects_outbox",
        ["effect_type"],
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_status", "decision_effects_outbox", ["status"]
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_next_attempt_at",
        "decision_effects_outbox",
        ["next_attempt_at"],
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_sent_at", "decision_effects_outbox", ["sent_at"]
    )
    _create_index_if_missing(
        "ix_decision_effects_outbox_updated_at",
        "decision_effects_outbox",
        ["updated_at"],
    )


def downgrade() -> None:
    _drop_index_if_exists(
        "ix_decision_effects_outbox_updated_at", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_sent_at", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_next_attempt_at", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_status", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_effect_type", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_issue_key", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_project_id", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_tenant_id", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_cycle_id", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_case_id", "decision_effects_outbox"
    )
    _drop_index_if_exists(
        "ix_decision_effects_outbox_dedupe_key", "decision_effects_outbox"
    )
    if _table_exists("decision_effects_outbox"):
        op.drop_table("decision_effects_outbox")

    _drop_index_if_exists("ix_decision_events_created_at", "decision_events")
    _drop_index_if_exists("ix_decision_events_outcome_state", "decision_events")
    _drop_index_if_exists("ix_decision_events_event_type", "decision_events")
    _drop_index_if_exists("ix_decision_events_source", "decision_events")
    _drop_index_if_exists("ix_decision_events_issue_key", "decision_events")
    _drop_index_if_exists("ix_decision_events_project_id", "decision_events")
    _drop_index_if_exists("ix_decision_events_tenant_id", "decision_events")
    _drop_index_if_exists("ix_decision_events_cycle_id", "decision_events")
    _drop_index_if_exists("ix_decision_events_case_id", "decision_events")
    _drop_index_if_exists("ix_decision_events_idempotency_key", "decision_events")
    if _table_exists("decision_events"):
        op.drop_table("decision_events")

    _drop_index_if_exists("ix_decision_cycles_updated_at", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_closed_at", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_opened_at", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_classification", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_status", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_issue_key", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_project_id", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_tenant_id", "decision_cycles")
    _drop_index_if_exists("ix_decision_cycles_case_id", "decision_cycles")
    if _table_exists("decision_cycles"):
        op.drop_table("decision_cycles")

    _drop_index_if_exists("ix_decision_cases_updated_at", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_last_event_at", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_last_event_type", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_last_source", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_active_cycle_id", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_state", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_issue_key", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_project_id", "decision_cases")
    _drop_index_if_exists("ix_decision_cases_tenant_id", "decision_cases")
    if _table_exists("decision_cases"):
        op.drop_table("decision_cases")
