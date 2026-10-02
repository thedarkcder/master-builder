"""enforce managed secrets scope with postgres row-level security

Revision ID: 20260211_0013
Revises: 20260210_0012
Create Date: 2026-02-11 12:35:00.000000
"""

from __future__ import annotations

from alembic import op


revision = "20260211_0013"
down_revision = "20260210_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("ALTER TABLE managed_secrets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE managed_secrets FORCE ROW LEVEL SECURITY")

    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_select ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_insert ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_update ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_delete ON managed_secrets")

    predicate = """
        (
            CASE current_setting('app.secret_scope', true)
                WHEN 'all' THEN true
                WHEN 'platform' THEN secret_ref LIKE 'platform/%'
                WHEN 'tenant' THEN (
                    current_setting('app.secret_tenant_id', true) IS NOT NULL
                    AND current_setting('app.secret_tenant_id', true) <> ''
                    AND secret_ref LIKE ('tenant/' || current_setting('app.secret_tenant_id', true) || '/%')
                )
                ELSE false
            END
        )
    """

    op.execute(
        f"""
        CREATE POLICY managed_secrets_scope_select
        ON managed_secrets
        FOR SELECT
        USING {predicate}
        """
    )
    op.execute(
        f"""
        CREATE POLICY managed_secrets_scope_insert
        ON managed_secrets
        FOR INSERT
        WITH CHECK {predicate}
        """
    )
    op.execute(
        f"""
        CREATE POLICY managed_secrets_scope_update
        ON managed_secrets
        FOR UPDATE
        USING {predicate}
        WITH CHECK {predicate}
        """
    )
    op.execute(
        f"""
        CREATE POLICY managed_secrets_scope_delete
        ON managed_secrets
        FOR DELETE
        USING {predicate}
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_delete ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_update ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_insert ON managed_secrets")
    op.execute("DROP POLICY IF EXISTS managed_secrets_scope_select ON managed_secrets")
    op.execute("ALTER TABLE managed_secrets NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE managed_secrets DISABLE ROW LEVEL SECURITY")
