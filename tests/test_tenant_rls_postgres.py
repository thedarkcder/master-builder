from __future__ import annotations

from datetime import datetime, timezone
import os
from uuid import uuid4
import secrets

from psycopg import sql
import psycopg

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DatabaseError

from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.tenant_rls import (
    assert_live_tenant_rls_contract,
    set_platform_admin_rls_context,
    set_platform_system_rls_context,
    set_tenant_system_rls_context,
    set_tenant_user_rls_context,
)


def _rls_test_url() -> str:
    url = os.environ.get("ORCHESTRATOR_RLS_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip(
            "ORCHESTRATOR_RLS_TEST_DATABASE_URL is required for PostgreSQL RLS integration tests"
        )
    return url


def _create_database(base_url: str) -> str:
    parsed = make_url(base_url)
    database_name = f"rls_test_{uuid4().hex}"
    admin_url = parsed.set(database="postgres")
    target_url = parsed.set(database=database_name)
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    admin_engine.dispose()
    return target_url.render_as_string(hide_password=False)


def _runtime_database_url(database_url: str) -> str:
    parsed = make_url(database_url)
    runtime_role = f"{parsed.database}_runtime"
    password = secrets.token_urlsafe(32)
    admin_url = (
        parsed.set(database="postgres")
        .render_as_string(hide_password=False)
        .replace("postgresql+psycopg:", "postgresql:")
    )
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOBYPASSRLS NOINHERIT NOCREATEDB NOCREATEROLE"
            ).format(sql.Identifier(runtime_role), sql.Literal(password))
        )
    with psycopg.connect(
        database_url.replace("postgresql+psycopg:", "postgresql:"), autocommit=True
    ) as connection:
        connection.execute(
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                sql.Identifier(runtime_role)
            )
        )
        connection.execute(
            sql.SQL(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}"
            ).format(sql.Identifier(runtime_role))
        )
        connection.execute(
            sql.SQL(
                "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}"
            ).format(sql.Identifier(runtime_role))
        )
    return parsed.set(username=runtime_role, password=password).render_as_string(
        hide_password=False
    )


def _drop_database(database_url: str) -> None:
    parsed = make_url(database_url)
    admin_url = parsed.set(database="postgres")
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(
            text(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = :database_name
                  AND pid <> pg_backend_pid()
                """
            ),
            {"database_name": parsed.database},
        )
        connection.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}"'))
        connection.execute(text(f'DROP ROLE IF EXISTS "{parsed.database}_runtime"'))
    admin_engine.dispose()


def _seed_two_tenants(connection) -> None:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    connection.execute(
        text(
            """
            INSERT INTO tenant_users (
                user_id, email, full_name, is_active, created_at, updated_at
            ) VALUES
                ('user-a', 'user-a@example.com', 'User A', true, :now, :now),
                ('user-b', 'user-b@example.com', 'User B', true, :now, :now)
            """
        ),
        {"now": now},
    )
    connection.execute(
        text(
            """
            INSERT INTO tenants (
                tenant_id, name, is_enabled, jira_config, github_config, repos_config,
                policy_config, experience_config, setup_state, created_at, updated_at
            ) VALUES
                ('tenant-a', 'Tenant A', true, '{}', '{}', '{}', '{}', '{}', '{}', :now, :now),
                ('tenant-b', 'Tenant B', true, '{}', '{}', '{}', '{}', '{}', '{}', :now, :now)
            """
        ),
        {"now": now},
    )
    connection.execute(
        text(
            """
            INSERT INTO atlassian_oauth_connections (
                connection_id, account_id, account_email, cloud_id, site_url, scopes,
                access_token_encrypted, refresh_token_encrypted, access_token_expires_at,
                created_at, updated_at
            ) VALUES (
                'conn-a', 'acct-a', 'a@example.com', 'cloud-a', 'https://example-1.atlassian.net',
                '[]', 'access-a', 'refresh-a', :now, :now, :now
            )
            """
        ),
        {"now": now},
    )
    connection.execute(
        text(
            "UPDATE tenants SET jira_config = '{\"connection_id\":\"conn-a\"}' WHERE tenant_id = 'tenant-a'"
        )
    )
    connection.execute(
        text(
            """
            INSERT INTO tenant_memberships (
                membership_id, tenant_id, user_id, role, onboarding_kind,
                discord_state, created_at, updated_at
            ) VALUES
                ('membership-a', 'tenant-a', 'user-a', 'tenant_admin', 'member_join', '{}', :now, :now),
                ('membership-b', 'tenant-b', 'user-b', 'tenant_admin', 'member_join', '{}', :now, :now)
            """
        ),
        {"now": now},
    )
    connection.execute(
        text(
            """
            INSERT INTO projects (
                project_id, tenant_id, name, github_repository, jira_project_key,
                policy_overrides, architecture_docs_config, environment, secret_refs,
                discord_config, is_archived, created_at, updated_at
            ) VALUES
                ('project-a', 'tenant-a', 'Project A', 'org/a', 'A', '{}', '{}', '{}', '{}', '{}', false, :now, :now),
                ('project-b', 'tenant-b', 'Project B', 'org/b', 'B', '{}', '{}', '{}', '{}', '{}', false, :now, :now)
            """
        ),
        {"now": now},
    )


@pytest.mark.production_path
def test_postgres_rls_enforces_tenant_membership_and_context() -> None:
    database_url = _create_database(_rls_test_url())
    try:
        run_migrations(database_url=database_url)
        engine = create_engine(_runtime_database_url(database_url), future=True)
        with engine.begin() as connection:
            role = connection.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
                )
            ).one()
            assert role == (False, False), (
                "RLS proof must use a runtime role that cannot bypass policies"
            )
            set_platform_system_rls_context(connection, system_purpose="rls_test_seed")
            _seed_two_tenants(connection)

        with engine.begin() as connection:
            set_tenant_user_rls_context(connection, user_id="user-a")
            rows = (
                connection.execute(
                    text("SELECT project_id FROM projects ORDER BY project_id")
                )
                .scalars()
                .all()
            )
            assert rows == ["project-a"]
            connection_rows = (
                connection.execute(
                    text(
                        "SELECT connection_id FROM atlassian_oauth_connections ORDER BY connection_id"
                    )
                )
                .scalars()
                .all()
            )
            assert connection_rows == ["conn-a"]

        with engine.begin() as connection:
            set_tenant_user_rls_context(
                connection, user_id="user-a", tenant_id="tenant-b"
            )
            rows = (
                connection.execute(
                    text("SELECT project_id FROM projects WHERE tenant_id = 'tenant-b'")
                )
                .scalars()
                .all()
            )
            assert rows == []
            connection_rows = (
                connection.execute(
                    text("SELECT connection_id FROM atlassian_oauth_connections")
                )
                .scalars()
                .all()
            )
            assert connection_rows == []
            with pytest.raises(DatabaseError):
                connection.execute(
                    text(
                        """
                        INSERT INTO projects (
                            project_id, tenant_id, name, github_repository, jira_project_key,
                            policy_overrides, architecture_docs_config, environment, secret_refs,
                            discord_config, is_archived, created_at, updated_at
                        ) VALUES (
                            'project-bad', 'tenant-b', 'Bad', 'org/bad', 'BAD',
                            '{}', '{}', '{}', '{}', '{}', false, now(), now()
                        )
                        """
                    )
                )

        with engine.begin() as connection:
            set_tenant_system_rls_context(
                connection, tenant_id="tenant-b", system_purpose="rls_test_worker"
            )
            rows = (
                connection.execute(
                    text("SELECT project_id FROM projects ORDER BY project_id")
                )
                .scalars()
                .all()
            )
            assert rows == ["project-b"]

        with engine.begin() as connection:
            set_platform_admin_rls_context(connection)
            rows = (
                connection.execute(
                    text("SELECT project_id FROM projects ORDER BY project_id")
                )
                .scalars()
                .all()
            )
            assert rows == ["project-a", "project-b"]
        engine.dispose()
    finally:
        _drop_database(database_url)


@pytest.mark.production_path
def test_postgres_catalog_has_forced_rls_and_required_policies() -> None:
    database_url = _create_database(_rls_test_url())
    try:
        run_migrations(database_url=database_url)
        engine = create_engine(_runtime_database_url(database_url), future=True)
        with engine.begin() as connection:
            assert_live_tenant_rls_contract(connection)
        engine.dispose()
    finally:
        _drop_database(database_url)


@pytest.mark.production_path
def test_postgres_session_identity_survives_transactions_without_pool_leakage(
    monkeypatch,
) -> None:
    from orchestrator.core.config import get_settings
    from orchestrator.storage.db import create_session_factory

    monkeypatch.setenv("ORCHESTRATOR_DB_POOL_SIZE", "1")
    monkeypatch.setenv("ORCHESTRATOR_DB_POOL_MAX_OVERFLOW", "0")
    get_settings.cache_clear()

    database_url = _create_database(_rls_test_url())
    engine = None
    try:
        run_migrations(database_url=database_url)
        factory = create_session_factory(_runtime_database_url(database_url))
        engine = factory.kw["bind"]
        with engine.begin() as connection:
            set_platform_system_rls_context(connection, system_purpose="rls_test_seed")
            _seed_two_tenants(connection)
        with factory() as session:
            set_tenant_user_rls_context(session, user_id="user-a", tenant_id="tenant-a")
            assert session.execute(
                text("SELECT project_id FROM projects")
            ).scalars().all() == ["project-a"]
            session.commit()
            assert session.execute(
                text("SELECT project_id FROM projects")
            ).scalars().all() == ["project-a"]
            session.rollback()
            assert session.execute(
                text("SELECT project_id FROM projects")
            ).scalars().all() == ["project-a"]
            set_tenant_user_rls_context(session, user_id="user-b", tenant_id="tenant-b")
            session.commit()
            assert session.execute(
                text("SELECT project_id FROM projects")
            ).scalars().all() == ["project-b"]
            for boundary in ("close", "reset", "invalidate"):
                set_tenant_user_rls_context(
                    session, user_id="user-b", tenant_id="tenant-b"
                )
                assert session.execute(
                    text("SELECT project_id FROM projects")
                ).scalars().all() == ["project-b"]
                getattr(session, boundary)()
                # A completed operation cannot retain identity on same-object reuse.
                assert (
                    session.execute(text("SELECT project_id FROM projects"))
                    .scalars()
                    .all()
                    == []
                )
        # The same pooled connection must not carry the preceding request's identity.
        with factory() as session:
            assert (
                session.execute(text("SELECT project_id FROM projects")).scalars().all()
                == []
            )
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(database_url)


@pytest.mark.production_path
def test_workflow_engine_session_factory_ends_its_identity_lifecycle() -> None:
    from orchestrator.core.config import get_settings
    from orchestrator.core.workflow.engine_factory import (
        create_session_factory_for_engine,
    )
    from orchestrator.storage.db import create_session_factory

    database_url = _create_database(_rls_test_url())
    engine = None
    try:
        run_migrations(database_url=database_url)
        factory = create_session_factory(_runtime_database_url(database_url))
        engine = factory.kw["bind"]
        with engine.begin() as connection:
            set_platform_system_rls_context(connection, system_purpose="rls_test_seed")
            _seed_two_tenants(connection)
        with factory() as owner:
            workflow_factory = create_session_factory_for_engine(
                session=owner, settings=get_settings()
            )
            session = workflow_factory()
            try:
                set_tenant_user_rls_context(
                    session, user_id="user-a", tenant_id="tenant-a"
                )
                assert session.execute(
                    text("SELECT project_id FROM projects")
                ).scalars().all() == ["project-a"]
                session.close()
                assert (
                    session.execute(text("SELECT project_id FROM projects"))
                    .scalars()
                    .all()
                    == []
                )
            finally:
                session.close()
    finally:
        if engine is not None:
            engine.dispose()
        _drop_database(database_url)


@pytest.mark.production_path
def test_runtime_guard_rejects_public_table_owner_with_altered_search_path() -> None:
    from orchestrator.storage.runtime_role import validate_runtime_connection

    database_url = _create_database(_rls_test_url())
    try:
        runtime_url = _runtime_database_url(database_url)
        runtime_role = make_url(runtime_url).username
        with psycopg.connect(
            database_url.replace("postgresql+psycopg:", "postgresql:"),
            autocommit=True,
        ) as owner:
            owner.execute("CREATE TABLE public.runtime_owned_probe (id integer)")
            owner.execute(
                sql.SQL("ALTER TABLE public.runtime_owned_probe OWNER TO {}").format(
                    sql.Identifier(runtime_role)
                )
            )
            owner.execute(
                sql.SQL("CREATE SCHEMA alternate AUTHORIZATION {}").format(
                    sql.Identifier(runtime_role)
                )
            )
        with psycopg.connect(
            runtime_url.replace("postgresql+psycopg:", "postgresql:"),
            options="-c search_path=alternate,public",
        ) as runtime:
            assert runtime.execute("SELECT current_schema()").fetchone() == (
                "alternate",
            )
            with pytest.raises(RuntimeError, match="non-owning runtime role"):
                validate_runtime_connection(runtime, None)
    finally:
        _drop_database(database_url)
