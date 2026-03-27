"""persist terminal decision gate closure state

Revision ID: 20260323_0035
Revises: 20260322_0034
Create Date: 2026-03-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260323_0035"
down_revision = "20260322_0034"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column.get("name") or "") for column in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("decision_cases"):
        return

    columns = _column_names("decision_cases")
    if "required_worker_label_present" not in columns:
        op.add_column(
            "decision_cases",
            sa.Column(
                "required_worker_label_present",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "decision_gate_closed_permanently" not in columns:
        op.add_column(
            "decision_cases",
            sa.Column(
                "decision_gate_closed_permanently",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "decision_gate_closed_at" not in columns:
        op.add_column(
            "decision_cases",
            sa.Column("decision_gate_closed_at", sa.DateTime(timezone=True), nullable=True),
        )
    if "decision_gate_closed_cycle_id" not in columns:
        op.add_column(
            "decision_cases",
            sa.Column("decision_gate_closed_cycle_id", sa.String(length=64), nullable=True),
        )

    if not _index_exists("decision_cases", "ix_decision_cases_decision_gate_closed_at"):
        op.create_index(
            "ix_decision_cases_decision_gate_closed_at",
            "decision_cases",
            ["decision_gate_closed_at"],
        )
    if not _index_exists("decision_cases", "ix_decision_cases_decision_gate_closed_cycle_id"):
        op.create_index(
            "ix_decision_cases_decision_gate_closed_cycle_id",
            "decision_cases",
            ["decision_gate_closed_cycle_id"],
        )


def downgrade() -> None:
    if not _table_exists("decision_cases"):
        return
    if _index_exists("decision_cases", "ix_decision_cases_decision_gate_closed_cycle_id"):
        op.drop_index("ix_decision_cases_decision_gate_closed_cycle_id", table_name="decision_cases")
    if _index_exists("decision_cases", "ix_decision_cases_decision_gate_closed_at"):
        op.drop_index("ix_decision_cases_decision_gate_closed_at", table_name="decision_cases")

    columns = _column_names("decision_cases")
    if "decision_gate_closed_cycle_id" in columns:
        op.drop_column("decision_cases", "decision_gate_closed_cycle_id")
    if "decision_gate_closed_at" in columns:
        op.drop_column("decision_cases", "decision_gate_closed_at")
    if "decision_gate_closed_permanently" in columns:
        op.drop_column("decision_cases", "decision_gate_closed_permanently")
    if "required_worker_label_present" in columns:
        op.drop_column("decision_cases", "required_worker_label_present")
