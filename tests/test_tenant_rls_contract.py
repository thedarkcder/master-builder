from __future__ import annotations

from orchestrator.storage.models import Base
from orchestrator.storage.tenant_rls import (
    CUSTOM_RLS_TABLES,
    IDENTITY_GLOBAL_TABLES,
    PLATFORM_GLOBAL_TABLES,
    TENANT_CHILD_TABLES,
    TENANT_SCOPED_TABLES,
    build_enable_rls_sql,
    build_policy_sql,
    build_rls_policies,
    rls_protected_tables,
    tenant_owned_tables,
)


def test_every_model_table_has_an_explicit_rls_classification() -> None:
    classified = (
        set(TENANT_SCOPED_TABLES)
        | set(TENANT_CHILD_TABLES)
        | set(IDENTITY_GLOBAL_TABLES)
        | set(PLATFORM_GLOBAL_TABLES)
        | set(CUSTOM_RLS_TABLES)
    )
    mapped = set(Base.metadata.tables)

    assert mapped - classified == set()
    assert classified - mapped == set()


def test_direct_tenant_id_tables_are_rls_protected_or_custom_classified() -> None:
    direct_tenant_tables = {
        name
        for name, table in Base.metadata.tables.items()
        if "tenant_id" in table.columns
    }

    assert direct_tenant_tables <= set(TENANT_SCOPED_TABLES) | set(CUSTOM_RLS_TABLES)
    assert "managed_secrets" in CUSTOM_RLS_TABLES


def test_tenant_owned_tables_are_not_classified_as_platform_global() -> None:
    owned = set(tenant_owned_tables())

    assert owned
    assert owned.isdisjoint(PLATFORM_GLOBAL_TABLES)
    assert "atlassian_oauth_connections" in owned
    assert "atlassian_oauth_connections" not in PLATFORM_GLOBAL_TABLES


def test_tenant_rls_migration_enables_forced_rls_for_every_tenant_owned_table() -> None:
    migration_sql = "\n".join(build_enable_rls_sql(rls_protected_tables()))

    for table_name in rls_protected_tables():
        quoted_name = f'"{table_name}"'
        assert f"ALTER TABLE {quoted_name} ENABLE ROW LEVEL SECURITY" in migration_sql
        assert f"ALTER TABLE {quoted_name} FORCE ROW LEVEL SECURITY" in migration_sql

    policy_sql = "\n".join(
        "\n".join(build_policy_sql(policy)) for policy in build_rls_policies()
    )
    assert "tenant_memberships" in policy_sql
    assert "app.tenant_id" in policy_sql
    assert "app.user_id" in policy_sql
    assert "tenant_system" in policy_sql
    assert "platform_admin" in policy_sql
