import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet

from orchestrator.api.admin.token_diagnostics_compare_service import get_token_stage_diagnostics_compare
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Run, RunLogEvent, Tenant


class TokenDiagnosticsCompareServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/token_diag_compare.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(self.database_url)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def test_compare_returns_stage_profiles_for_multiple_projects(self) -> None:
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            session.add_all(
                [
                    Project(
                        project_id="project-1",
                        tenant_id="tenant-a",
                        name="Project One",
                        github_repository="https://github.com/example/project-one",
                        jira_project_key="P1",
                        policy_overrides={},
                        environment={},
                        secret_refs={},
                        discord_config={},
                        is_archived=False,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                    Project(
                        project_id="project-2",
                        tenant_id="tenant-a",
                        name="Project Two",
                        github_repository="https://github.com/example/project-two",
                        jira_project_key="P2",
                        policy_overrides={},
                        environment={},
                        secret_refs={},
                        discord_config={},
                        is_archived=False,
                        created_at=self.now,
                        updated_at=self.now,
                    ),
                ]
            )
            session.add_all(
                [
                    Run(
                        run_id="run-1",
                        tenant_id="tenant-a",
                        project_id="project-1",
                        issue_key="P1-1",
                        issue_summary="summary",
                        issue_description="description",
                        repo_url="https://github.com/example/project-one",
                        branch="jira/P1-1",
                        pr_url=None,
                        status="completed",
                        last_error=None,
                        plan=None,
                        created_at=self.now,
                        started_at=self.now,
                        finished_at=self.now,
                    ),
                    Run(
                        run_id="run-2",
                        tenant_id="tenant-a",
                        project_id="project-2",
                        issue_key="P2-1",
                        issue_summary="summary",
                        issue_description="description",
                        repo_url="https://github.com/example/project-two",
                        branch="jira/P2-1",
                        pr_url=None,
                        status="completed",
                        last_error=None,
                        plan=None,
                        created_at=self.now,
                        started_at=self.now,
                        finished_at=self.now,
                    ),
                ]
            )
            session.add_all(
                [
                    RunLogEvent(
                        event_id="event-1",
                        tenant_id="tenant-a",
                        project_id="project-1",
                        run_id="run-1",
                        issue_key="P1-1",
                        agent_id="agent-1",
                        invocation_id="inv-1",
                        channel="codex",
                        command="npm run test",
                        working_dir="/workspace",
                        stage="test",
                        attempt=1,
                        stream="stdout",
                        message='{"type":"turn.completed","turn_id":"turn-1","usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":10}}',
                        recorded_at=self.now,
                    ),
                    RunLogEvent(
                        event_id="event-2",
                        tenant_id="tenant-a",
                        project_id="project-2",
                        run_id="run-2",
                        issue_key="P2-1",
                        agent_id="agent-1",
                        invocation_id="inv-2",
                        channel="codex",
                        command="npm run test",
                        working_dir="/workspace",
                        stage="test",
                        attempt=1,
                        stream="stdout",
                        message='{"type":"turn.completed","turn_id":"turn-2","usage":{"input_tokens":150,"cached_input_tokens":30,"output_tokens":20}}',
                        recorded_at=self.now,
                    ),
                ]
            )
            session.commit()

        with self.session_factory() as session:
            payload = get_token_stage_diagnostics_compare(
                session=session,
                tenant_id="tenant-a",
                project_ids="project-1,project-2",
            )

        self.assertEqual(len(payload.projects), 2)
        self.assertEqual(payload.projects[0].project_id, "project-1")
        self.assertEqual(payload.projects[1].project_id, "project-2")
        self.assertTrue(any(stage.stage == "test" for stage in payload.projects[0].stages))
        self.assertTrue(any(stage.stage == "test" for stage in payload.projects[1].stages))


if __name__ == "__main__":
    unittest.main()
