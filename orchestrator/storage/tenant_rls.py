from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session


class RLSPrincipalType(StrEnum):
    TENANT_USER = "tenant_user"
    TENANT_SYSTEM = "tenant_system"
    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_SYSTEM = "platform_system"
    IDENTITY_AUTH = "identity_auth"


@dataclass(frozen=True)
class RLSContractIssue:
    table_name: str
    issue: str


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


def direct_tenant_table_names_from_metadata() -> tuple[str, ...]:
    from orchestrator.storage.models import Base

    return tuple(
        sorted(
            table_name
            for table_name, table in Base.metadata.tables.items()
            if "tenant_id" in table.columns
        )
    )


def nullable_tenant_id_tables_from_metadata() -> tuple[str, ...]:
    from orchestrator.storage.models import Base

    return tuple(
        sorted(
            table_name
            for table_name, table in Base.metadata.tables.items()
            if "tenant_id" in table.columns and table.columns["tenant_id"].nullable
        )
    )


def direct_tenant_table_names_from_database(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
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


def nullable_tenant_id_tables_from_database(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND column_name = 'tenant_id'
              AND is_nullable = 'YES'
              AND table_name NOT LIKE 'alembic_%'
            ORDER BY table_name
            """
        )
    ).scalars()
    return tuple(rows)


def annotated_rls_protected_table_names_from_database(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
        text(
            """
            SELECT c.relname
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_description d ON d.objoid = c.oid AND d.objsubid = 0
            WHERE n.nspname = current_schema()
              AND c.relkind IN ('r', 'p')
              AND d.description LIKE '%tenant_rls:protected%'
            ORDER BY c.relname
            """
        )
    ).scalars()
    return tuple(rows)


def rls_protected_table_names_from_database(connection: Connection) -> tuple[str, ...]:
    return tuple(
        sorted(
            set(direct_tenant_table_names_from_database(connection))
            | set(annotated_rls_protected_table_names_from_database(connection))
        )
    )


def audit_live_tenant_rls_contract(connection: Connection) -> tuple[RLSContractIssue, ...]:
    protected_tables = rls_protected_table_names_from_database(connection)
    issues: list[RLSContractIssue] = [
        RLSContractIssue(table_name=table_name, issue="tenant_id is nullable")
        for table_name in nullable_tenant_id_tables_from_database(connection)
    ]
    if not protected_tables:
        return tuple(issues)

    catalog_rows = {
        row["relname"]: row
        for row in connection.execute(
            text(
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = ANY(:table_names)
                  AND relkind IN ('r', 'p')
                """
            ),
            {"table_names": list(protected_tables)},
        ).mappings()
    }
    policy_tables = set(
        connection.execute(
            text(
                """
                SELECT DISTINCT tablename
                FROM pg_policies
                WHERE schemaname = current_schema()
                  AND tablename = ANY(:table_names)
                """
            ),
            {"table_names": list(protected_tables)},
        ).scalars()
    )
    for table_name in protected_tables:
        row = catalog_rows.get(table_name)
        if row is None:
            issues.append(RLSContractIssue(table_name=table_name, issue="missing pg_class row"))
            continue
        if not row["relrowsecurity"]:
            issues.append(RLSContractIssue(table_name=table_name, issue="RLS is not enabled"))
        if not row["relforcerowsecurity"]:
            issues.append(RLSContractIssue(table_name=table_name, issue="RLS is not forced"))
        if table_name not in policy_tables:
            issues.append(RLSContractIssue(table_name=table_name, issue="missing RLS policy"))
    return tuple(issues)


def assert_live_tenant_rls_contract(connection: Connection) -> None:
    issues = audit_live_tenant_rls_contract(connection)
    if issues:
        detail = "; ".join(f"{issue.table_name}: {issue.issue}" for issue in issues)
        raise RuntimeError(f"Tenant RLS contract failed: {detail}")
