"""widen pr review publication review ids to bigint

Revision ID: 20260327_0041
Revises: 20260323_0038
Create Date: 2026-03-27 19:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260327_0041"
down_revision = "20260323_0038"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in set(inspector.get_table_names())


def upgrade() -> None:
    if not _has_table("pr_review_publications"):
        return
    with op.batch_alter_table("pr_review_publications", recreate="auto") as batch_op:
        batch_op.alter_column(
            "review_id",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=True,
        )


def downgrade() -> None:
    if not _has_table("pr_review_publications"):
        return
    with op.batch_alter_table("pr_review_publications", recreate="auto") as batch_op:
        batch_op.alter_column(
            "review_id",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=True,
        )
