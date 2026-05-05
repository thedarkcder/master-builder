"""Enforce stage-scoped planning decision record identity.

Revision ID: 20260504_0105
Revises: 20260504_0104
Create Date: 2026-05-04 21:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260504_0105"
down_revision = "20260504_0104"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _index_exists(bind: sa.engine.Connection, table_name: str, index_name: str) -> bool:
    return any(index["name"] == index_name for index in sa.inspect(bind).get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "planning_decision_records"):
        return

    op.execute(
        """
        UPDATE planning_decision_records
        SET source_stage = 'unknown'
        WHERE source_stage IS NULL OR trim(source_stage) = ''
        """
    )
    op.execute(
        """
        DELETE FROM planning_decision_records
        WHERE record_id IN (
            SELECT record_id
            FROM (
                SELECT
                    record_id,
                    row_number() OVER (
                        PARTITION BY tenant_id, workflow_id, lane, source_stage, external_key
                        ORDER BY updated_at DESC, created_at DESC, record_id DESC
                    ) AS duplicate_rank
                FROM planning_decision_records
            ) ranked_records
            WHERE duplicate_rank > 1
        )
        """
    )

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE planning_decision_records ALTER COLUMN source_stage SET NOT NULL")

    if not _index_exists(bind, "planning_decision_records", "uq_planning_decision_records_identity"):
        op.create_index(
            "uq_planning_decision_records_identity",
            "planning_decision_records",
            ["tenant_id", "workflow_id", "lane", "source_stage", "external_key"],
            unique=True,
        )


def downgrade() -> None:
    raise RuntimeError("Planning decision record identity migration cannot be downgraded")
