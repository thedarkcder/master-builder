import pytest


def test_runtime_role_rejects_privilege_and_ownership():
    from orchestrator.storage.runtime_role import validate_runtime_role_flags

    validate_runtime_role_flags(
        superuser=False,
        bypass_rls=False,
        owns_database=False,
        owns_protected_tables=False,
        privileged_membership=False,
    )
    for flag in (
        "superuser",
        "bypass_rls",
        "owns_database",
        "owns_protected_tables",
        "privileged_membership",
    ):
        flags = dict(
            superuser=False,
            bypass_rls=False,
            owns_database=False,
            owns_protected_tables=False,
            privileged_membership=False,
        )
        flags[flag] = True
        with pytest.raises(RuntimeError, match="non-owning runtime role"):
            validate_runtime_role_flags(**flags)


def test_runtime_configuration_requires_explicit_database(monkeypatch):
    from orchestrator.core.config import Settings
    from pydantic import ValidationError

    monkeypatch.delenv("ORCHESTRATOR_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


def test_sqlite_requires_explicit_test_opt_in(monkeypatch):
    from orchestrator.storage.db import create_db_engine
    from orchestrator.core.config import get_settings

    monkeypatch.setenv("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS", "false")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="PostgreSQL"):
        create_db_engine()


@pytest.mark.parametrize(
    "field,value",
    [
        ("db_pool_size", 0),
        ("db_pool_max_overflow", -1),
        ("db_pool_timeout_seconds", 0),
        ("db_pool_recycle_seconds", -1),
    ],
)
def test_runtime_pool_configuration_rejects_invalid_values(field, value):
    from orchestrator.core.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match=field):
        Settings(**{field: value})


def test_runtime_guard_audits_canonical_public_schema_despite_changed_search_path():
    from orchestrator.storage.runtime_role import validate_runtime_connection

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, query):
            # Model an owning runtime role selecting an empty schema first.
            self.owns_public_table = "n.nspname = 'public'" in query
            assert "current_schema()" not in query, (
                "Caller-controlled search_path must not select the schema audited"
            )

        def fetchone(self):
            return False, False, False, self.owns_public_table, False

    class Connection:
        rolled_back = False

        def cursor(self):
            return Cursor()

        def rollback(self):
            self.rolled_back = True

    connection = Connection()
    with pytest.raises(RuntimeError, match="non-owning runtime role"):
        validate_runtime_connection(connection, None)
    assert connection.rolled_back
