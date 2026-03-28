"""compatibility revision for merged alembic history

Revision ID: 20260327_0042
Revises: 20260327_0041
Create Date: 2026-03-28 01:56:00.000000
"""

from __future__ import annotations


revision = "20260327_0042"
down_revision = "20260327_0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This branch already creates run_stream_events in 20260327_0040.
    # Keep the compatibility branch empty so merged-head databases upgrade cleanly
    # without replaying the same schema change under a second revision id.
    pass


def downgrade() -> None:
    pass
