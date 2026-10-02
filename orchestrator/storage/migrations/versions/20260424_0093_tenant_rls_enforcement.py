"""Enforce database tenant isolation with PostgreSQL RLS.

Revision ID: 20260424_0093
Revises: 20260424_0092
Create Date: 2026-04-24 19:00:00.000000
"""

from dataclasses import dataclass
from typing import Iterable

from alembic import op
from sqlalchemy import text


revision = "20260424_0093"
down_revision = "20260424_0092"
branch_labels = None
depends_on = None


@dataclass(frozen=True)
class _Policy:
    table_name: str
    policy_name: str
    using_expression: str
    check_expression: str | None = None


_DERIVED_TENANT_SCOPES: dict[str, tuple[str, str]] = {
    "project_automation_executions": (
        "SELECT pa.tenant_id FROM project_automations pa "
        "WHERE pa.automation_id = project_automation_executions.automation_id",
        "Execution rows are tenant-owned through their project automation.",
    ),
    "workflow_checkpoints": (
        "SELECT we.tenant_id FROM workflow_executions we "
        "WHERE we.workflow_id = workflow_checkpoints.workflow_id",
        "Checkpoint rows are tenant-owned through their workflow execution.",
    ),
    "workflow_operations": (
        "SELECT we.tenant_id FROM workflow_executions we "
        "WHERE we.workflow_id = workflow_operations.workflow_id",
        "Operation rows are tenant-owned through their workflow execution.",
    ),
    "workflow_operation_attempts": (
        "SELECT we.tenant_id FROM workflow_operations wo "
        "JOIN workflow_executions we ON we.workflow_id = wo.workflow_id "
        "WHERE wo.operation_id = workflow_operation_attempts.operation_id",
        "Attempt rows are tenant-owned through their workflow operation.",
    ),
}

_CUSTOM_DERIVED_POLICY_REASONS: dict[str, str] = {
    "atlassian_oauth_connections": "Connection rows are tenant-owned through the tenant Jira connection binding.",
    "managed_secrets": "Secret rows are tenant-owned through the tenant secret reference prefix.",
    "tenant_team_memberships": "Team membership rows are tenant-owned through membership and team joins.",
}

_IDENTITY_POLICY_REASONS: dict[str, str] = {
    "tenant_users": "User identity rows are scoped by authenticated user_id.",
    "tenant_user_credentials": "Credential rows are scoped by authenticated user_id.",
    "tenant_user_discord_identities": "Discord identity rows are scoped by authenticated user_id.",
}

_CUSTOM_DIRECT_POLICY_TABLES = {"tenant_memberships"}


def _quote_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum() or identifier[0].isdigit():
        raise ValueError(f"Unsafe SQL identifier: {identifier}")
    return f'"{identifier}"'


def _direct_tenant_tables() -> tuple[str, ...]:
    bind = op.get_bind()
    rows = bind.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND column_name = 'tenant_id'
              AND table_name NOT LIKE 'alembic_%'
            ORDER BY table_name
            """
        )
    ).scalars()
    return tuple(rows)


def _existing_tables(table_names: Iterable[str]) -> tuple[str, ...]:
    names = tuple(sorted(set(table_names)))
    if not names:
        return ()
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
            {"table_names": list(names)},
        )
        .scalars()
    )
    return tuple(rows)


def _tenant_access_expression(row_tenant_sql: str) -> str:
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


def _direct_policy(table_name: str) -> _Policy:
    return _Policy(
        table_name=table_name,
        policy_name=f"{table_name}_tenant_isolation",
        using_expression=_tenant_access_expression(
            f"{_quote_identifier(table_name)}.tenant_id"
        ),
    )


def _derived_policy(table_name: str, tenant_scope_sql: str) -> _Policy:
    return _Policy(
        table_name=table_name,
        policy_name=f"{table_name}_tenant_isolation",
        using_expression=_tenant_access_expression(f"({tenant_scope_sql})"),
    )


def _custom_policies() -> tuple[_Policy, ...]:
    return (
        _Policy(
            table_name="tenant_memberships",
            policy_name="tenant_memberships_tenant_isolation",
            using_expression="""(
    current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system')
    OR (
        current_setting('app.principal_type', true) = 'tenant_system'
        AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
        AND tenant_memberships.tenant_id = current_setting('app.tenant_id', true)
    )
    OR (
        current_setting('app.principal_type', true) = 'tenant_user'
        AND tenant_memberships.user_id = current_setting('app.user_id', true)
        AND (
            NULLIF(current_setting('app.tenant_id', true), '') IS NULL
            OR tenant_memberships.tenant_id = current_setting('app.tenant_id', true)
        )
    )
)""",
        ),
        _Policy(
            table_name="tenant_team_memberships",
            policy_name="tenant_team_memberships_tenant_isolation",
            using_expression="""(
    current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system')
    OR EXISTS (
        SELECT 1
        FROM tenant_memberships tm
        JOIN tenant_teams tt ON tt.tenant_id = tm.tenant_id
        WHERE tm.membership_id = tenant_team_memberships.membership_id
          AND tt.team_id = tenant_team_memberships.team_id
          AND (
              (
                  current_setting('app.principal_type', true) = 'tenant_system'
                  AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
                  AND tm.tenant_id = current_setting('app.tenant_id', true)
              )
              OR (
                  current_setting('app.principal_type', true) = 'tenant_user'
                  AND tm.user_id = current_setting('app.user_id', true)
                  AND (
                      NULLIF(current_setting('app.tenant_id', true), '') IS NULL
                      OR tm.tenant_id = current_setting('app.tenant_id', true)
                  )
              )
          )
    )
)""",
        ),
        _Policy(
            table_name="managed_secrets",
            policy_name="managed_secrets_tenant_isolation",
            using_expression="""(
    current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system')
    OR (
        current_setting('app.principal_type', true) = 'tenant_system'
        AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
        AND managed_secrets.secret_ref LIKE ('tenant/' || current_setting('app.tenant_id', true) || '/%')
    )
    OR (
        current_setting('app.principal_type', true) = 'tenant_user'
        AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
        AND managed_secrets.secret_ref LIKE ('tenant/' || current_setting('app.tenant_id', true) || '/%')
        AND EXISTS (
            SELECT 1
            FROM tenant_memberships tm
            WHERE tm.tenant_id = current_setting('app.tenant_id', true)
              AND tm.user_id = current_setting('app.user_id', true)
        )
    )
)""",
        ),
        _Policy(
            table_name="atlassian_oauth_connections",
            policy_name="atlassian_oauth_connections_tenant_isolation",
            using_expression=f"""(
    current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system')
    OR EXISTS (
        SELECT 1
        FROM tenants t
        WHERE t.jira_config ->> 'connection_id' = atlassian_oauth_connections.connection_id
          AND {_tenant_access_expression("t.tenant_id")}
    )
)""",
        ),
    )


def _identity_policies() -> tuple[_Policy, ...]:
    owner_expression = """(
    current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system', 'identity_auth')
    OR (
        current_setting('app.principal_type', true) = 'tenant_user'
        AND user_id = current_setting('app.user_id', true)
    )
)"""
    return tuple(
        _Policy(
            table_name=table_name,
            policy_name=f"{table_name}_identity_isolation",
            using_expression=owner_expression,
        )
        for table_name in _IDENTITY_POLICY_REASONS
    )


def _policies() -> tuple[_Policy, ...]:
    direct = tuple(
        _direct_policy(table_name)
        for table_name in _direct_tenant_tables()
        if table_name not in _CUSTOM_DIRECT_POLICY_TABLES
    )
    derived = tuple(
        _derived_policy(table_name, scope_sql)
        for table_name, (scope_sql, _reason) in _DERIVED_TENANT_SCOPES.items()
    )
    return direct + derived + _custom_policies() + _identity_policies()


def _policy_sql(policy: _Policy) -> tuple[str, str]:
    quoted_table = _quote_identifier(policy.table_name)
    quoted_policy = _quote_identifier(policy.policy_name)
    check_expression = policy.check_expression or policy.using_expression
    return (
        f"DROP POLICY IF EXISTS {quoted_policy} ON {quoted_table}",
        f"""CREATE POLICY {quoted_policy}
ON {quoted_table}
FOR ALL
USING {policy.using_expression}
WITH CHECK {check_expression}""",
    )


def _protected_tables() -> tuple[str, ...]:
    direct = _direct_tenant_tables()
    annotated = (
        tuple(_DERIVED_TENANT_SCOPES)
        + tuple(_CUSTOM_DERIVED_POLICY_REASONS)
        + tuple(_IDENTITY_POLICY_REASONS)
    )
    return _existing_tables(set(direct) | set(annotated))


def _comment_protected_tables() -> None:
    for table_name, (_scope_sql, reason) in _DERIVED_TENANT_SCOPES.items():
        op.execute(
            f"COMMENT ON TABLE {_quote_identifier(table_name)} IS 'tenant_rls:protected; {reason}'"
        )
    for table_name, reason in (
        _CUSTOM_DERIVED_POLICY_REASONS | _IDENTITY_POLICY_REASONS
    ).items():
        op.execute(
            f"COMMENT ON TABLE {_quote_identifier(table_name)} IS 'tenant_rls:protected; {reason}'"
        )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    _comment_protected_tables()

    for table_name in _protected_tables():
        quoted_table = _quote_identifier(table_name)
        op.execute(f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY")

    for policy in _policies():
        if policy.table_name not in _protected_tables():
            continue
        drop_policy_sql, create_policy_sql = _policy_sql(policy)
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

    for policy in _policies():
        drop_policy_sql, _ = _policy_sql(policy)
        op.execute(drop_policy_sql)

    for table_name in _protected_tables():
        quoted_table = _quote_identifier(table_name)
        op.execute(f"ALTER TABLE {quoted_table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {quoted_table} DISABLE ROW LEVEL SECURITY")
