"""managed secrets table

Revision ID: 20260207_0005
Revises: 20260207_0004
Create Date: 2026-02-07 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260207_0005"
down_revision = "20260207_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "managed_secrets",
        sa.Column("secret_ref", sa.String(length=255), nullable=False),
        sa.Column("value_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("secret_ref"),
    )


def downgrade() -> None:
    op.drop_table("managed_secrets")
