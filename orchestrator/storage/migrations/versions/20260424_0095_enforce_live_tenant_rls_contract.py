"""Enforce tenant RLS from live database catalog.

Revision ID: 20260424_0095
Revises: 20260424_0094
Create Date: 2026-04-24 22:30:00.000000
"""

from __future__ import annotations

from alembic import op

from orchestrator.storage.tenant_rls import (
    assert_live_tenant_rls_contract,
    nullable_tenant_id_tables_from_database,
    rls_protected_table_names_from_database,
)


revision = "20260424_0095"
down_revision = "20260424_0094"
branch_labels = None
depends_on = None


def _delete_tenantless_rows() -> None:
    op.execute("DELETE FROM admin_notifications WHERE tenant_id IS NULL")
    op.execute("DELETE FROM webhook_jobs WHERE tenant_id IS NULL")


def _enforce_tenant_id_not_null() -> None:
    for table_name in nullable_tenant_id_tables_from_database(op.get_bind()):
        op.execute(f'ALTER TABLE "{table_name}" ALTER COLUMN tenant_id SET NOT NULL')


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _delete_tenantless_rows()
    _enforce_tenant_id_not_null()

    for table_name in rls_protected_table_names_from_database(bind):
        op.execute(f'ALTER TABLE "{table_name}" ENABLE ROW LEVEL SECURITY')
        op.execute(f'ALTER TABLE "{table_name}" FORCE ROW LEVEL SECURITY')

    assert_live_tenant_rls_contract(bind)


def downgrade() -> None:
    raise RuntimeError("Tenant RLS enforcement cannot be downgraded")
