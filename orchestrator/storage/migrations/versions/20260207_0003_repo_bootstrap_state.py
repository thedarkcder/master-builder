"""repo bootstrap state tracking

Revision ID: 20260207_0003
Revises: 20260206_0002
Create Date: 2026-02-07
"""

from alembic import op
import sqlalchemy as sa


revision = "20260207_0003"
down_revision = "20260206_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "repo_bootstrap_states",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=False),
        sa.Column("last_branch", sa.String(length=255), nullable=True),
        sa.Column("last_run_id", sa.String(length=64), nullable=True),
        sa.Column("last_created_files", sa.JSON(), nullable=False),
        sa.Column("bootstrap_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "bootstrapped_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tenant_id", "repo_url"),
    )


def downgrade() -> None:
    op.drop_table("repo_bootstrap_states")
