"""Enforce database tenant isolation with PostgreSQL RLS.

Revision ID: 20260424_0093
Revises: 20260424_0092
Create Date: 2026-04-24 19:00:00.000000
"""

from __future__ import annotations

from alembic import op

from orchestrator.storage.tenant_rls import (
    build_enable_rls_sql,
    build_policy_sql,
    build_rls_policies,
    rls_protected_tables,
)


revision = "20260424_0093"
down_revision = "20260424_0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for statement in build_enable_rls_sql(rls_protected_tables()):
        op.execute(statement)

    for policy in build_rls_policies():
        drop_policy_sql, create_policy_sql = build_policy_sql(policy)
        op.execute(drop_policy_sql)
        op.execute(create_policy_sql)

    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_select ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_insert ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_update ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_delete ON managed_secrets")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for policy in build_rls_policies():
        drop_policy_sql, _ = build_policy_sql(policy)
        op.execute(drop_policy_sql)

    for table_name in rls_protected_tables():
        quoted_table = f'"{table_name}"'
        op.execute(f"ALTER TABLE {quoted_table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {quoted_table} DISABLE ROW LEVEL SECURITY")
