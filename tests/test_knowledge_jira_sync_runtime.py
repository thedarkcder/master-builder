from __future__ import annotations

from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.knowledge_jira_sync_runtime import (
    KnowledgeJiraSyncDependencyFailure,
    KnowledgeJiraSyncRuntime,
    _classify_project_failure,
    get_knowledge_jira_sync_runtime_status,
)
from orchestrator.api.jira_oauth.service import execute_jira_operation_with_refresh_retry
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.tools.jira_oauth_models import JiraOAuthAuthRequiredError, JiraOAuthError, JiraOAuthHttpError


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        knowledge_jira_auto_sync_enabled=True,
        knowledge_jira_sync_interval_seconds=3600,
        knowledge_jira_sync_poll_seconds=30,
        knowledge_jira_sync_max_issues=200,
        knowledge_jira_sync_invalid_token_backoff_seconds=21600,
        codex_reasoning_effort="medium",
    )


def _seed_sync_project(database_url: str) -> None:
    session_factory = create_session_factory(database_url)
    with session_factory() as session:
        now = datetime.now(timezone.utc)
        session.add(
            Tenant(
                tenant_id="tenant-1",
                name="Tenant",
                is_enabled=True,
                jira_config={"connection_id": "conn-1"},
                github_config={},
                repos_config={},
                policy_config={"knowledge_base_enabled": True},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            Project(
                project_id="project-1",
                tenant_id="tenant-1",
                name="Sync Me",
                github_repository="example/repo",
                jira_project_key="GP",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            JiraOAuthConnection(
                connection_id="conn-1",
                account_id="acct",
                account_email="user@example.com",
                cloud_id="cloud-1",
                site_url="https://example.atlassian.net",
                scopes=["read:jira-work"],
                access_token_encrypted="enc",
                refresh_token_encrypted="ref",
                access_token_expires_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()


def test_list_sync_projects_filters_to_enabled_kb_projects() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={"connection_id": "conn-1"},
                    github_config={},
                    repos_config={},
                    policy_config={"knowledge_base_enabled": True},
                    discord_config={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Tenant(
                    tenant_id="tenant-2",
                    name="Tenant 2",
                    is_enabled=True,
                    jira_config={"connection_id": "conn-2"},
                    github_config={},
                    repos_config={},
                    policy_config={"knowledge_base_enabled": True},
                    discord_config={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add_all(
                [
                    Project(
                        project_id="project-1",
                        tenant_id="tenant-1",
                        name="Sync Me",
                        github_repository="example/repo",
                        jira_project_key="GP",
                        policy_overrides={},
                        environment={},
                        secret_refs={},
                        discord_config={},
                        is_archived=False,
                        created_at=now,
                        updated_at=now,
                    ),
                    Project(
                        project_id="project-2",
                        tenant_id="tenant-1",
                        name="Archived",
                        github_repository="example/repo-archived",
                        jira_project_key="OLD",
                        policy_overrides={},
                        environment={},
                        secret_refs={},
                        discord_config={},
                        is_archived=True,
                        created_at=now,
                        updated_at=now,
                    ),
                    Project(
                        project_id="project-3",
                        tenant_id="tenant-2",
                        name="KB Disabled",
                        github_repository="example/repo",
                        jira_project_key="NOPE",
                        policy_overrides={"knowledge_base_enabled": False},
                        environment={},
                        secret_refs={},
                        discord_config={},
                        is_archived=False,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            session.commit()

            runtime = KnowledgeJiraSyncRuntime(settings=_settings(database_url))
            projects = runtime._list_sync_projects(session)

        assert [(project.tenant_id, project.project_id, project.jira_project_key) for project in projects] == [
            ("tenant-1", "project-1", "GP")
        ]


def test_run_forever_requires_postgres() -> None:
    runtime = KnowledgeJiraSyncRuntime(settings=_settings("sqlite:///tmp/test.db"))
    with patch.object(runtime, "_write_runtime_status"):
        try:
            runtime.run_forever()
        except KnowledgeJiraSyncDependencyFailure as exc:
            assert "requires PostgreSQL" in str(exc)
        else:  # pragma: no cover - defensive assertion
            raise AssertionError("Expected non-Postgres runtime to be rejected")


def test_run_sync_pass_persists_runtime_and_project_status() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_sync.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        _seed_sync_project(database_url)
        runtime = KnowledgeJiraSyncRuntime(settings=_settings(database_url))
        fake_result = SimpleNamespace(
            created_assets=2,
            updated_assets=1,
            unchanged_assets=0,
            deleted_assets=0,
            skipped_assets=0,
            failed_assets=0,
        )

        with (
            patch("orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens", return_value="access-token") as refresh_mock,
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
            patch("orchestrator.core.knowledge_jira_sync_runtime.sync_project_knowledge_from_jira", return_value=fake_result) as sync_mock,
        ):
            runtime._run_sync_pass()

        refresh_mock.assert_called_once()
        sync_mock.assert_called_once()
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            snapshot = get_knowledge_jira_sync_runtime_status(session=session, settings=_settings(database_url))
        assert snapshot.state == "running"
        assert snapshot.service_instance_id is not None
        assert snapshot.last_pass_started_at is not None
        assert snapshot.last_pass_finished_at is not None
        assert snapshot.last_heartbeat_at is not None
        assert len(snapshot.projects) == 1
        assert snapshot.projects[0].state == "healthy"
        assert snapshot.projects[0].failure_category is None


def test_execute_jira_operation_with_refresh_retry_retries_once_on_auth_failure() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_retry.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        _seed_sync_project(database_url)
        session_factory = create_session_factory(database_url)
        settings = _settings(database_url)
        fake_result = SimpleNamespace(
            created_assets=1,
            updated_assets=0,
            unchanged_assets=0,
            deleted_assets=0,
            skipped_assets=0,
            failed_assets=0,
        )

        operation_calls: list[str] = []

        def _operation(session, client, access_token: str):  # noqa: ANN001
            del session, client
            operation_calls.append(access_token)
            if len(operation_calls) == 1:
                raise JiraOAuthHttpError(
                    "Jira API request failed (401): unauthorized",
                    status_code=401,
                    error_prefix="Jira API request failed",
                    error_body="unauthorized",
                )
            return fake_result

        with (
            patch("orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens", side_effect=["token-1", "token-2"]) as refresh_mock,
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
        ):
            result = execute_jira_operation_with_refresh_retry(
                session_factory=session_factory,
                settings=settings,
                connection_id="conn-1",
                tenant_id="tenant-1",
                project_id="project-1",
                operation=_operation,
            )

        assert result == fake_result
        assert operation_calls == ["token-1", "token-2"]
        assert refresh_mock.call_count == 2
        assert refresh_mock.call_args_list[1].kwargs["force_refresh"] is True


def test_run_sync_pass_marks_auth_required_as_degraded_with_backoff() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_auth_required.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        _seed_sync_project(database_url)
        runtime = KnowledgeJiraSyncRuntime(settings=_settings(database_url))

        with (
            patch("orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens", side_effect=["access-token-1", "access-token-2"]),
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
            patch(
                "orchestrator.core.knowledge_jira_sync_runtime.sync_project_knowledge_from_jira",
                side_effect=[
                    JiraOAuthHttpError(
                        "Jira API request failed (401): unauthorized",
                        status_code=401,
                        error_prefix="Jira API request failed",
                        error_body="unauthorized",
                    ),
                    JiraOAuthHttpError(
                        "Jira API request failed (403): forbidden",
                        status_code=403,
                        error_prefix="Jira API request failed",
                        error_body="forbidden",
                    ),
                ],
            ),
        ):
            runtime._run_sync_pass()

        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            snapshot = get_knowledge_jira_sync_runtime_status(session=session, settings=_settings(database_url))
        assert snapshot.state == "degraded"
        assert len(snapshot.projects) == 1
        status = snapshot.projects[0]
        assert status.state == "degraded"
        assert status.failure_category == "auth_required"
        assert status.next_retry_at is not None
        assert status.consecutive_failures == 1


def test_run_sync_pass_marks_invalid_refresh_token_as_degraded_with_backoff() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_invalid_token.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        _seed_sync_project(database_url)
        runtime = KnowledgeJiraSyncRuntime(settings=_settings(database_url))

        with (
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
            patch(
                "orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens",
                side_effect=JiraOAuthError('Jira OAuth request failed (403): {"error":"unauthorized_client","error_description":"refresh_token is invalid"}'),
            ),
        ):
            runtime._run_sync_pass()

        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            snapshot = get_knowledge_jira_sync_runtime_status(session=session, settings=_settings(database_url))
        assert snapshot.state == "degraded"
        assert len(snapshot.projects) == 1
        status = snapshot.projects[0]
        assert status.state == "degraded"
        assert status.failure_category == "invalid_refresh_token"
        assert status.next_retry_at is not None
        assert status.consecutive_failures == 1


def test_run_sync_pass_skips_invalid_refresh_token_project_during_backoff() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_invalid_skip.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        _seed_sync_project(database_url)
        runtime = KnowledgeJiraSyncRuntime(settings=_settings(database_url))

        with (
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
            patch(
                "orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens",
                side_effect=JiraOAuthError('Jira OAuth request failed (403): {"error":"unauthorized_client","error_description":"refresh_token is invalid"}'),
            ),
        ):
            runtime._run_sync_pass()

        with (
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
            patch(
                "orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens",
                side_effect=AssertionError("refresh should be skipped during backoff"),
            ),
        ):
            runtime._run_sync_pass()

        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            snapshot = get_knowledge_jira_sync_runtime_status(session=session, settings=_settings(database_url))
        assert len(snapshot.projects) == 1
        assert snapshot.projects[0].consecutive_failures == 1


def test_run_sync_pass_skips_auth_required_project_during_backoff() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_auth_skip.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        _seed_sync_project(database_url)
        runtime = KnowledgeJiraSyncRuntime(settings=_settings(database_url))

        with (
            patch("orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens", side_effect=["access-token-1", "access-token-2"]),
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
            patch(
                "orchestrator.core.knowledge_jira_sync_runtime.sync_project_knowledge_from_jira",
                side_effect=[
                    JiraOAuthHttpError(
                        "Jira API request failed (401): unauthorized",
                        status_code=401,
                        error_prefix="Jira API request failed",
                        error_body="unauthorized",
                    ),
                    JiraOAuthHttpError(
                        "Jira API request failed (403): forbidden",
                        status_code=403,
                        error_prefix="Jira API request failed",
                        error_body="forbidden",
                    ),
                ],
            ),
        ):
            runtime._run_sync_pass()

        with (
            patch("orchestrator.api.jira_oauth.service.jira_oauth_client", return_value=SimpleNamespace()),
            patch(
                "orchestrator.api.jira_oauth.service.refresh_jira_connection_tokens",
                side_effect=AssertionError("refresh should be skipped during auth_required backoff"),
            ),
        ):
            runtime._run_sync_pass()

        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            snapshot = get_knowledge_jira_sync_runtime_status(session=session, settings=_settings(database_url))
        assert len(snapshot.projects) == 1
        assert snapshot.projects[0].failure_category == "auth_required"
        assert snapshot.projects[0].consecutive_failures == 1


def test_get_runtime_status_marks_stale_runtime() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_stale.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        settings = _settings(database_url)
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
            from orchestrator.core.knowledge_jira_sync_status import upsert_runtime_status

            upsert_runtime_status(
                session=session,
                settings=settings,
                state="running",
                last_heartbeat_at=datetime.now(timezone.utc) - timedelta(hours=3),
                leader_acquired=False,
                service_instance_id="svc:1",
            )
            session.commit()

        with session_factory() as session:
            snapshot = get_knowledge_jira_sync_runtime_status(session=session, settings=settings)

        assert snapshot.state == "stale"
        assert snapshot.stale is True


def test_classify_invalid_refresh_token_failure() -> None:
    category = _classify_project_failure(
        JiraOAuthError('Jira OAuth request failed (403): {"error":"unauthorized_client","error_description":"refresh_token is invalid"}')
    )
    assert category == "invalid_refresh_token"


def test_classify_auth_required_failure_from_typed_error() -> None:
    category = _classify_project_failure(JiraOAuthAuthRequiredError("Jira OAuth authorization is required"))
    assert category == "auth_required"
