from __future__ import annotations

from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DatabaseError

from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.tenant_rls import (
    build_rls_policies,
    rls_protected_tables,
    set_platform_admin_rls_context,
    set_platform_system_rls_context,
    set_tenant_system_rls_context,
    set_tenant_user_rls_context,
)


def _rls_test_url() -> str:
    url = os.environ.get("ORCHESTRATOR_RLS_TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.skip("ORCHESTRATOR_RLS_TEST_DATABASE_URL is required for PostgreSQL RLS integration tests")
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
    return str(target_url)


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
                'conn-a', 'acct-a', 'a@example.com', 'cloud-a', 'https://example.atlassian.net',
                '[]', 'access-a', 'refresh-a', :now, :now, :now
            )
            """
        ),
        {"now": now},
    )
    connection.execute(
        text("UPDATE tenants SET jira_config = '{\"connection_id\":\"conn-a\"}' WHERE tenant_id = 'tenant-a'")
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
        engine = create_engine(database_url, future=True)
        with engine.begin() as connection:
            set_platform_system_rls_context(connection, system_purpose="rls_test_seed")
            _seed_two_tenants(connection)

        with engine.begin() as connection:
            set_tenant_user_rls_context(connection, user_id="user-a")
            rows = connection.execute(text("SELECT project_id FROM projects ORDER BY project_id")).scalars().all()
            assert rows == ["project-a"]
            connection_rows = connection.execute(
                text("SELECT connection_id FROM atlassian_oauth_connections ORDER BY connection_id")
            ).scalars().all()
            assert connection_rows == ["conn-a"]

        with engine.begin() as connection:
            set_tenant_user_rls_context(connection, user_id="user-a", tenant_id="tenant-b")
            rows = connection.execute(
                text("SELECT project_id FROM projects WHERE tenant_id = 'tenant-b'")
            ).scalars().all()
            assert rows == []
            connection_rows = connection.execute(
                text("SELECT connection_id FROM atlassian_oauth_connections")
            ).scalars().all()
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
            set_tenant_system_rls_context(connection, tenant_id="tenant-b", system_purpose="rls_test_worker")
            rows = connection.execute(text("SELECT project_id FROM projects ORDER BY project_id")).scalars().all()
            assert rows == ["project-b"]

        with engine.begin() as connection:
            set_platform_admin_rls_context(connection)
            rows = connection.execute(text("SELECT project_id FROM projects ORDER BY project_id")).scalars().all()
            assert rows == ["project-a", "project-b"]
        engine.dispose()
    finally:
        _drop_database(database_url)


@pytest.mark.production_path
def test_postgres_catalog_has_forced_rls_and_required_policies() -> None:
    database_url = _create_database(_rls_test_url())
    try:
        run_migrations(database_url=database_url)
        engine = create_engine(database_url, future=True)
        expected_policies = {policy.policy_name for policy in build_rls_policies()}
        with engine.begin() as connection:
            catalog_rows = connection.execute(
                text(
                    """
                    SELECT relname, relrowsecurity, relforcerowsecurity
                    FROM pg_class
                    WHERE relname = ANY(:table_names)
                    """
                ),
                {"table_names": list(rls_protected_tables())},
            ).mappings().all()
            assert {row["relname"] for row in catalog_rows} == set(rls_protected_tables())
            assert all(row["relrowsecurity"] for row in catalog_rows)
            assert all(row["relforcerowsecurity"] for row in catalog_rows)

            policy_names = set(
                connection.execute(
                    text("SELECT policyname FROM pg_policies WHERE policyname = ANY(:policy_names)"),
                    {"policy_names": list(expected_policies)},
                ).scalars()
            )
            assert policy_names == expected_policies
        engine.dispose()
    finally:
        _drop_database(database_url)
