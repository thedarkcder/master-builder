"""Merge provider-backed secret crypto migration with current workflow head."""

from __future__ import annotations

from alembic import op

revision = "20260507_0107"
down_revision = ("20260414_0065", "20260505_0106")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind()


def downgrade() -> None:
    op.get_bind()
