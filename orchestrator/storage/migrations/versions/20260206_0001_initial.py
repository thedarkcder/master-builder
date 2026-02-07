"""initial schema

Revision ID: 20260206_0001
Revises:
Create Date: 2026-02-06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260206_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("jira_config", sa.JSON(), nullable=False),
        sa.Column("github_config", sa.JSON(), nullable=False),
        sa.Column("repos_config", sa.JSON(), nullable=False),
        sa.Column("policy_config", sa.JSON(), nullable=False),
        sa.Column("discord_config", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
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
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=False),
        sa.Column("repo_url", sa.String(length=512), nullable=True),
        sa.Column("branch", sa.String(length=255), nullable=True),
        sa.Column("pr_url", sa.String(length=512), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("plan", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
    )

    op.create_index("ix_runs_issue_key", "runs", ["issue_key"], unique=False)
    op.create_index("ix_runs_status", "runs", ["status"], unique=False)
    op.create_index("ix_runs_tenant_id", "runs", ["tenant_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_runs_tenant_id", table_name="runs")
    op.drop_index("ix_runs_status", table_name="runs")
    op.drop_index("ix_runs_issue_key", table_name="runs")
    op.drop_table("runs")
    op.drop_table("tenants")
