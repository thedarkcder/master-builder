"""Remove Postgres raw event transport tables.

Revision ID: 20260424_0094
Revises: 20260424_0093
Create Date: 2026-04-24 22:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260424_0094"
down_revision = "20260424_0093"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _drop_table_if_exists(table_name: str) -> None:
    if _table_exists(table_name):
        op.drop_table(table_name)


def upgrade() -> None:
    _drop_table_if_exists("observability_stream_events")
    _drop_table_if_exists("audit_events")
    _drop_table_if_exists("run_stream_events")
    _drop_table_if_exists("run_log_events")


def downgrade() -> None:
    raise RuntimeError("Postgres raw event transport tables were intentionally removed")
