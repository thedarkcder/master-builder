from __future__ import annotations

from orchestrator.storage.models import Base
from orchestrator.storage.tenant_rls import (
    direct_tenant_table_names_from_metadata,
    nullable_tenant_id_tables_from_metadata,
)


def test_tenant_id_tables_are_discovered_from_metadata_not_manual_allowlist() -> None:
    discovered = set(direct_tenant_table_names_from_metadata())
    metadata_tenant_tables = {
        table_name
        for table_name, table in Base.metadata.tables.items()
        if "tenant_id" in table.columns
    }

    assert discovered == metadata_tenant_tables
    assert "admin_notifications" in discovered
    assert "webhook_jobs" in discovered


def test_tenant_id_tables_are_not_nullable() -> None:
    assert nullable_tenant_id_tables_from_metadata() == ()


def test_rls_policy_contract_is_not_owned_by_runtime_module() -> None:
    runtime_exports = set(
        dir(__import__("orchestrator.storage.tenant_rls", fromlist=["*"]))
    )

    assert "build_rls_policies" not in runtime_exports
    assert "TENANT_SCOPED_TABLES" not in runtime_exports
    assert "CUSTOM_RLS_TABLES" not in runtime_exports
