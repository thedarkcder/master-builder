"""compatibility revision for merged alembic history

Revision ID: 20260327_0041
Revises: 20260323_0038
Create Date: 2026-03-28 01:55:00.000000
"""

from __future__ import annotations


revision = "20260327_0041"
down_revision = "20260323_0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This branch already applies the real review-id widening via 20260327_0039.
    # Keep this revision as a no-op so databases stamped on the merged 0043 head
    # remain resolvable on branches that do not include the tenant-access line.
    pass


def downgrade() -> None:
    pass
