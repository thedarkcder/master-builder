"""Add durable planning decision records.

Revision ID: 20260502_0102
Revises: 20260430_0101
Create Date: 2026-05-02 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260502_0102"
down_revision = "20260430_0101"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _apply_postgres_rls(bind: sa.engine.Connection) -> None:
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "COMMENT ON TABLE planning_decision_records IS 'tenant_rls:protected; durable planning decision audit'"
    )
    op.execute("ALTER TABLE planning_decision_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE planning_decision_records FORCE ROW LEVEL SECURITY")
    op.execute(
        "DROP POLICY IF EXISTS planning_decision_records_tenant_isolation ON planning_decision_records"
    )
    op.execute(
        """
        CREATE POLICY planning_decision_records_tenant_isolation
        ON planning_decision_records
        USING (tenant_id = current_setting('app.current_tenant_id', true))
        WITH CHECK (tenant_id = current_setting('app.current_tenant_id', true))
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "planning_decision_records"):
        _apply_postgres_rls(bind)
        return
    op.create_table(
        "planning_decision_records",
        sa.Column("record_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(length=128),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            sa.String(length=128),
            sa.ForeignKey("projects.project_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "workflow_id",
            sa.String(length=128),
            sa.ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "source_operation_id",
            sa.String(length=64),
            sa.ForeignKey("workflow_operations.operation_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "source_attempt_id",
            sa.String(length=64),
            sa.ForeignKey(
                "workflow_operation_attempts.attempt_id", ondelete="SET NULL"
            ),
            nullable=True,
        ),
        sa.Column("parent_issue_key", sa.String(length=64), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("source_stage", sa.String(length=64), nullable=True),
        sa.Column("external_key", sa.String(length=128), nullable=False),
        sa.Column(
            "payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
    )
    op.create_index(
        "ix_planning_decision_records_tenant_workflow",
        "planning_decision_records",
        ["tenant_id", "workflow_id"],
    )
    op.create_index(
        "ix_planning_decision_records_tenant_issue",
        "planning_decision_records",
        ["tenant_id", "parent_issue_key"],
    )
    op.create_index(
        "ix_planning_decision_records_tenant_lane_status",
        "planning_decision_records",
        ["tenant_id", "lane", "status"],
    )
    op.create_index(
        "ix_planning_decision_records_source_attempt",
        "planning_decision_records",
        ["source_attempt_id"],
    )
    op.create_index(
        "ix_planning_decision_records_tenant_id",
        "planning_decision_records",
        ["tenant_id"],
    )
    op.create_index(
        "ix_planning_decision_records_project_id",
        "planning_decision_records",
        ["project_id"],
    )
    op.create_index(
        "ix_planning_decision_records_workflow_id",
        "planning_decision_records",
        ["workflow_id"],
    )
    op.create_index(
        "ix_planning_decision_records_source_operation_id",
        "planning_decision_records",
        ["source_operation_id"],
    )
    op.create_index(
        "ix_planning_decision_records_parent_issue_key",
        "planning_decision_records",
        ["parent_issue_key"],
    )
    op.create_index(
        "ix_planning_decision_records_lane", "planning_decision_records", ["lane"]
    )
    op.create_index(
        "ix_planning_decision_records_status", "planning_decision_records", ["status"]
    )
    op.create_index(
        "ix_planning_decision_records_source_stage",
        "planning_decision_records",
        ["source_stage"],
    )
    op.create_index(
        "ix_planning_decision_records_external_key",
        "planning_decision_records",
        ["external_key"],
    )
    op.create_index(
        "ix_planning_decision_records_created_at",
        "planning_decision_records",
        ["created_at"],
    )
    op.create_index(
        "ix_planning_decision_records_updated_at",
        "planning_decision_records",
        ["updated_at"],
    )
    _apply_postgres_rls(bind)


def downgrade() -> None:
    raise RuntimeError("Planning decision records migration cannot be downgraded")
