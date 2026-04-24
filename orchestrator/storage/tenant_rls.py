from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session


class RLSPrincipalType(StrEnum):
    TENANT_USER = "tenant_user"
    TENANT_SYSTEM = "tenant_system"
    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_SYSTEM = "platform_system"
    IDENTITY_AUTH = "identity_auth"


TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "tenants",
    "tenant_invites",
    "projects",
    "architecture_documents",
    "project_installs",
    "project_install_requests",
    "project_automations",
    "admin_notifications",
    "workflow_executions",
    "runs",
    "run_human_input_requests",
    "audit_events",
    "pm_interview_cases",
    "followup_contexts",
    "tenant_run_claims",
    "webhook_deliveries",
    "webhook_jobs",
    "pr_review_publications",
    "repo_bootstrap_states",
    "agent_lifecycle_events",
    "run_log_events",
    "run_stream_events",
    "observability_stream_events",
    "run_token_usage",
    "knowledge_assets",
    "knowledge_chunks",
    "knowledge_facts",
    "knowledge_sources",
    "knowledge_jira_sync_project_states",
    "decision_cases",
    "decision_cycles",
    "decision_answers",
    "decision_evidence",
    "decision_events",
    "decision_effects_outbox",
)

TENANT_CHILD_TABLES: tuple[str, ...] = (
    "project_automation_executions",
    "workflow_checkpoints",
    "workflow_operations",
    "workflow_operation_attempts",
)

CUSTOM_RLS_TABLES: tuple[str, ...] = (
    "managed_secrets",
    "tenant_memberships",
    "tenant_teams",
    "tenant_team_memberships",
)

IDENTITY_GLOBAL_TABLES: tuple[str, ...] = (
    "tenant_users",
    "tenant_user_credentials",
    "tenant_user_discord_identities",
)

PLATFORM_GLOBAL_TABLES: tuple[str, ...] = (
    "atlassian_oauth_connections",
    "platform_settings",
    "workflow_types",
    "workflow_type_operations",
    "webhook_subject_claims",
    "knowledge_jira_sync_runtime_states",
    "discord_command_sync_runtime_states",
    "worker_runtime_states",
    "worker_runtime_auth_requests",
)

TENANT_CHILD_SCOPE_SQL: dict[str, str] = {
    "project_automation_executions": (
        "SELECT pa.tenant_id FROM project_automations pa "
        "WHERE pa.automation_id = project_automation_executions.automation_id"
    ),
    "workflow_checkpoints": (
        "SELECT we.tenant_id FROM workflow_executions we "
        "WHERE we.workflow_id = workflow_checkpoints.workflow_id"
    ),
    "workflow_operations": (
        "SELECT we.tenant_id FROM workflow_executions we "
        "WHERE we.workflow_id = workflow_operations.workflow_id"
    ),
    "workflow_operation_attempts": (
        "SELECT we.tenant_id FROM workflow_operations wo "
        "JOIN workflow_executions we ON we.workflow_id = wo.workflow_id "
        "WHERE wo.operation_id = workflow_operation_attempts.operation_id"
    ),
}


@dataclass(frozen=True)
class RLSPolicy:
    table_name: str
    policy_name: str
    using_expression: str
    check_expression: str | None = None


def tenant_owned_tables() -> tuple[str, ...]:
    return TENANT_SCOPED_TABLES + TENANT_CHILD_TABLES + CUSTOM_RLS_TABLES


def rls_protected_tables() -> tuple[str, ...]:
    return tenant_owned_tables() + IDENTITY_GLOBAL_TABLES


def set_rls_context(
    session: Session | Connection,
    *,
    principal_type: RLSPrincipalType | str,
    tenant_id: str | None = None,
    user_id: str | None = None,
    system_purpose: str | None = None,
) -> None:
    bind = getattr(session, "bind", None)
    dialect = bind.dialect if bind is not None else getattr(session, "dialect", None)
    if dialect is None or dialect.name != "postgresql":
        return

    principal = RLSPrincipalType(str(principal_type))
    if principal is RLSPrincipalType.TENANT_USER and not user_id:
        raise ValueError("tenant_user RLS context requires user_id")
    if principal is RLSPrincipalType.TENANT_SYSTEM and (not tenant_id or not system_purpose):
        raise ValueError("tenant_system RLS context requires tenant_id and system_purpose")

    session.execute(text("SELECT set_config('app.principal_type', :value, true)"), {"value": principal.value})
    session.execute(text("SELECT set_config('app.tenant_id', :value, true)"), {"value": tenant_id or ""})
    session.execute(text("SELECT set_config('app.user_id', :value, true)"), {"value": user_id or ""})
    session.execute(text("SELECT set_config('app.system_purpose', :value, true)"), {"value": system_purpose or ""})


def set_platform_admin_rls_context(session: Session | Connection) -> None:
    set_rls_context(session, principal_type=RLSPrincipalType.PLATFORM_ADMIN)


def set_platform_system_rls_context(session: Session | Connection, *, system_purpose: str) -> None:
    if not system_purpose:
        raise ValueError("platform_system RLS context requires system_purpose")
    set_rls_context(
        session,
        principal_type=RLSPrincipalType.PLATFORM_SYSTEM,
        system_purpose=system_purpose,
    )


def set_tenant_user_rls_context(session: Session | Connection, *, user_id: str, tenant_id: str | None = None) -> None:
    set_rls_context(
        session,
        principal_type=RLSPrincipalType.TENANT_USER,
        user_id=user_id,
        tenant_id=tenant_id,
    )


def set_identity_auth_rls_context(session: Session | Connection) -> None:
    set_rls_context(session, principal_type=RLSPrincipalType.IDENTITY_AUTH)


def set_tenant_system_rls_context(session: Session | Connection, *, tenant_id: str, system_purpose: str) -> None:
    set_rls_context(
        session,
        principal_type=RLSPrincipalType.TENANT_SYSTEM,
        tenant_id=tenant_id,
        system_purpose=system_purpose,
    )


def _quote_identifier(identifier: str) -> str:
    if not identifier.replace("_", "").isalnum() or identifier[0].isdigit():
        raise ValueError(f"Unsafe SQL identifier: {identifier}")
    return f'"{identifier}"'


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


def _direct_tenant_policy(table_name: str) -> RLSPolicy:
    return RLSPolicy(
        table_name=table_name,
        policy_name=f"{table_name}_tenant_isolation",
        using_expression=_tenant_access_expression(f"{_quote_identifier(table_name)}.tenant_id"),
    )


def _child_tenant_policy(table_name: str, tenant_scope_sql: str) -> RLSPolicy:
    scoped_tenant = f"({tenant_scope_sql})"
    return RLSPolicy(
        table_name=table_name,
        policy_name=f"{table_name}_tenant_isolation",
        using_expression=_tenant_access_expression(scoped_tenant),
    )


def _custom_policies() -> tuple[RLSPolicy, ...]:
    membership_policy = RLSPolicy(
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
    )
    team_policy = RLSPolicy(
        table_name="tenant_teams",
        policy_name="tenant_teams_tenant_isolation",
        using_expression=_tenant_access_expression("tenant_teams.tenant_id"),
    )
    team_membership_policy = RLSPolicy(
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
    )
    managed_secrets_policy = RLSPolicy(
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
    )
    return (membership_policy, team_policy, team_membership_policy, managed_secrets_policy)


def _identity_policies() -> tuple[RLSPolicy, ...]:
    owner_expression = """(
    current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system', 'identity_auth')
    OR (
        current_setting('app.principal_type', true) = 'tenant_user'
        AND user_id = current_setting('app.user_id', true)
    )
)"""
    return tuple(
        RLSPolicy(
            table_name=table_name,
            policy_name=f"{table_name}_identity_isolation",
            using_expression=owner_expression,
        )
        for table_name in IDENTITY_GLOBAL_TABLES
    )


def build_rls_policies() -> tuple[RLSPolicy, ...]:
    direct = tuple(_direct_tenant_policy(table_name) for table_name in TENANT_SCOPED_TABLES)
    children = tuple(
        _child_tenant_policy(table_name, tenant_scope_sql)
        for table_name, tenant_scope_sql in TENANT_CHILD_SCOPE_SQL.items()
    )
    return direct + children + _custom_policies() + _identity_policies()


def build_enable_rls_sql(table_names: Iterable[str]) -> tuple[str, ...]:
    statements: list[str] = []
    for table_name in table_names:
        quoted_table = _quote_identifier(table_name)
        statements.append(f"ALTER TABLE {quoted_table} ENABLE ROW LEVEL SECURITY")
        statements.append(f"ALTER TABLE {quoted_table} FORCE ROW LEVEL SECURITY")
    return tuple(statements)


def build_policy_sql(policy: RLSPolicy) -> tuple[str, str]:
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
