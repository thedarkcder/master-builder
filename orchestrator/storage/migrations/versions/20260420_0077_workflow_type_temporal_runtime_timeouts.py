"""retained no-op after removing temporal engine config from workflow types

Revision ID: 20260420_0077
Revises: 20260417_0076
Create Date: 2026-04-20 15:20:00.000000
"""

from __future__ import annotations


revision = "20260420_0077"
down_revision = "20260417_0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    return None


def downgrade() -> None:
    return None
