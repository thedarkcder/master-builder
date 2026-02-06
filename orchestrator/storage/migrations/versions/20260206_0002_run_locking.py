"""run locking and webhook idempotency

Revision ID: 20260206_0002
Revises: 20260206_0001
Create Date: 2026-02-06
"""

from alembic import op
import sqlalchemy as sa


revision = "20260206_0002"
down_revision = "20260206_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_locks",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "issue_key"),
    )
    op.create_index("ix_run_locks_run_id", "run_locks", ["run_id"], unique=True)

    op.create_table(
        "webhook_deliveries",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "delivery_id"),
    )
    op.create_index("ix_webhook_deliveries_run_id", "webhook_deliveries", ["run_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_webhook_deliveries_run_id", table_name="webhook_deliveries")
    op.drop_table("webhook_deliveries")
    op.drop_index("ix_run_locks_run_id", table_name="run_locks")
    op.drop_table("run_locks")
