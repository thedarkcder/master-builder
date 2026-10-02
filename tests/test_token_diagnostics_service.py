import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet

from orchestrator.api.admin.token_diagnostics_service import get_token_stage_diagnostics
from orchestrator.core.config import get_settings
from orchestrator.core.observability.logging_pane import emit_logging_pane_event
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Tenant
from tests.workflow_test_support import add_run_with_workflow, make_run


class TokenDiagnosticsServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/token_diag.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = (
            Fernet.generate_key().decode("utf-8")
        )
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

    def test_stage_diagnostics_reports_qa_stage_usage(self) -> None:
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
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-a",
                    name="Project One",
                    github_repository="https://github.com/example/project-one",
                    jira_project_key="P1",
                    policy_overrides={"qa_demo_recording_enabled": True},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
            add_run_with_workflow(
                session,
                make_run(
                    run_id="run-1",
                    workflow_id="workflow-run-1",
                    tenant_id="tenant-a",
                    project_id="project-1",
                    issue_key="P1-1",
                    issue_summary="summary",
                    issue_description="description",
                    repo_url="https://github.com/example/project-one",
                    branch="jira/P1-1",
                    attempt_number=1,
                    entry_mode="fresh",
                    entry_stage="orchestrated",
                    status="completed",
                    created_at=self.now,
                    started_at=self.now,
                    finished_at=self.now,
                ),
            )
            emit_logging_pane_event(
                session=session,
                tenant_id="tenant-a",
                project_id="project-1",
                run_id="run-1",
                issue_key="P1-1",
                agent_id="agent-1",
                invocation_id="inv-qa",
                channel="codex",
                command="qa demo",
                working_dir="/workspace",
                stage="qa",
                attempt=1,
                stream="stdout",
                message=(
                    '{"type":"turn.completed","turn_id":"turn-qa",'
                    '"usage":{"input_tokens":220,"cached_input_tokens":20,"output_tokens":30}}'
                ),
                recorded_at=self.now,
            )
            session.commit()

        with self.session_factory() as session:
            payload = get_token_stage_diagnostics(
                session=session,
                tenant_id="tenant-a",
                project_id="project-1",
            )

        stages = {stage.stage: stage for stage in payload.stages}
        self.assertIn("qa", stages)
        self.assertEqual(stages["qa"].run_count, 1)
        self.assertEqual(stages["qa"].avg_delta, 220)


if __name__ == "__main__":
    unittest.main()
