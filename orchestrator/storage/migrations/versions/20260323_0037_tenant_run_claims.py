"""add dedicated tenant run claim lock table

Revision ID: 20260323_0037
Revises: 20260323_0036
Create Date: 2026-03-23
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "20260323_0037"
down_revision = "20260323_0036"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("tenant_run_claims"):
        op.create_table(
            "tenant_run_claims",
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("tenant_id"),
        )

    bind = op.get_bind()
    tenant_ids = [row[0] for row in bind.execute(sa.text("SELECT tenant_id FROM tenants")).fetchall()]
    if not tenant_ids:
        return

    now = datetime.now(timezone.utc)
    tenant_claims = sa.table(
        "tenant_run_claims",
        sa.column("tenant_id", sa.String(length=128)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    existing_ids = {
        row[0] for row in bind.execute(sa.text("SELECT tenant_id FROM tenant_run_claims")).fetchall()
    }
    rows = [
        {"tenant_id": tenant_id, "updated_at": now}
        for tenant_id in tenant_ids
        if tenant_id not in existing_ids
    ]
    if rows:
        op.bulk_insert(tenant_claims, rows)


def downgrade() -> None:
    if _table_exists("tenant_run_claims"):
        op.drop_table("tenant_run_claims")
