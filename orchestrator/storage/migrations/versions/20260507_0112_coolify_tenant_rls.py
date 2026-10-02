"""Enable tenant RLS for Coolify deployment tables.

Revision ID: 20260507_0112
Revises: 20260507_0111
Create Date: 2026-05-07 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import text


revision = "20260507_0112"
down_revision = "20260507_0111"
branch_labels = None
depends_on = None

_TENANT_TABLES = (
    "project_apps",
    "project_app_analysis_runs",
    "project_deployment_releases",
    "project_deployment_restore_runs",
    "deployment_host_commands",
)


def _quote_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum() or identifier[0].isdigit():
        raise ValueError(f"Unsafe SQL identifier: {identifier}")
    return f'"{identifier}"'


def _existing_tables() -> tuple[str, ...]:
    rows = (
        op.get_bind()
        .execute(
            text(
                """
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = current_schema()
              AND tablename = ANY(:table_names)
            ORDER BY tablename
            """
            ),
            {"table_names": list(_TENANT_TABLES)},
        )
        .scalars()
    )
    return tuple(rows)


def _tenant_access_expression(table_name: str) -> str:
    row_tenant_sql = f"{_quote_identifier(table_name)}.tenant_id"
    return f"""(
    current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system')
    OR (
        current_setting('app.principal_type', true) = 'tenant_system'
        AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
        AND {row_tenant_sql} = current_setting('app.tenant_id', true)
    )
    OR (
        current_setting('app.principal_type', true) = 'tenant_user'
        AND NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
        AND {row_tenant_sql} IN (
            SELECT tm.tenant_id
            FROM tenant_memberships tm
            WHERE tm.user_id = current_setting('app.user_id', true)
        )
        AND (
            NULLIF(current_setting('app.tenant_id', true), '') IS NULL
            OR {row_tenant_sql} = current_setting('app.tenant_id', true)
        )
    )
)"""


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in _existing_tables():
        quoted_table = _quote_identifier(table_name)
        quoted_policy = _quote_identifier(f"{table_name}_tenant_isolation")
        policy_expression = _tenant_access_expression(table_name)
        op.execute(f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS {quoted_policy} ON {quoted_table}")
        op.execute(
            f"""CREATE POLICY {quoted_policy}
ON {quoted_table}
FOR ALL
USING {policy_expression}
WITH CHECK {policy_expression}"""
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    for table_name in _existing_tables():
        quoted_table = _quote_identifier(table_name)
        quoted_policy = _quote_identifier(f"{table_name}_tenant_isolation")
        op.execute(f"DROP POLICY IF EXISTS {quoted_policy} ON {quoted_table}")
        op.execute(f"ALTER TABLE {quoted_table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {quoted_table} DISABLE ROW LEVEL SECURITY")
