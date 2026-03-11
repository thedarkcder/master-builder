from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.knowledge_jira_sync_runtime import KnowledgeJiraSyncRuntime
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant


def _settings(database_url: str) -> Settings:
    return Settings(
        database_url=database_url,
        knowledge_jira_auto_sync_enabled=True,
        knowledge_jira_sync_interval_seconds=3600,
        knowledge_jira_sync_poll_seconds=30,
        knowledge_jira_sync_max_issues=200,
        codex_model="gpt-5.4",
        codex_reasoning_effort="medium",
    )


def test_list_sync_projects_filters_to_enabled_kb_projects() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
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
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
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
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
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
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
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
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
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
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    ),
                ]
            )
            session.commit()

            runtime = KnowledgeJiraSyncRuntime(settings=_settings(database_url))
            projects = runtime._list_sync_projects(session)

        assert [(project.tenant_id, project.project_id, project.jira_project_key) for project in projects] == [
            ("tenant-1", "project-1", "GP")
        ]


def test_run_sync_pass_executes_sync_for_eligible_project() -> None:
    with TemporaryDirectory() as temp_dir:
        database_url = f"sqlite:///{temp_dir}/runtime_sync.db"
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url)
        with session_factory() as session:
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
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
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
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
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
                    access_token_expires_at=datetime.now(timezone.utc),
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

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
            patch("orchestrator.core.knowledge_jira_sync_runtime.refresh_jira_connection_tokens", return_value="access-token") as refresh_mock,
            patch("orchestrator.core.knowledge_jira_sync_runtime.jira_oauth_client", return_value=SimpleNamespace()) as client_mock,
            patch("orchestrator.core.knowledge_jira_sync_runtime.sync_project_knowledge_from_jira", return_value=fake_result) as sync_mock,
        ):
            runtime._run_sync_pass()

        refresh_mock.assert_called_once()
        client_mock.assert_called_once()
        sync_mock.assert_called_once()
        kwargs = sync_mock.call_args.kwargs
        assert kwargs["tenant_id"] == "tenant-1"
        assert kwargs["project_id"] == "project-1"
        assert kwargs["project_key"] == "GP"
        assert kwargs["access_token"] == "access-token"
        assert kwargs["cloud_id"] == "cloud-1"
        assert kwargs["max_issues"] == 200
