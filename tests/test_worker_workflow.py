import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from orchestrator.core.config import get_settings
from orchestrator.core.workflow_runner import (
    PmPlan,
    WorkflowDiagnostics,
    WorkflowResult,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Run, Tenant
from orchestrator.worker import process_next_queued_run


class _SuccessRunner:
    def __init__(self) -> None:
        self.last_request = None

    def run(self, request):  # noqa: ANN001
        self.last_request = request
        return WorkflowResult(
            succeeded=True,
            plan=PmPlan(
                plan_steps=["plan", "build", "validate"],
                acceptance_criteria=["has PR link"],
                risks=[],
            ),
            pr_url="https://github.com/example/repo/pull/99",
            summary=["workflow completed"],
            test_guidance=["python3 -m unittest discover -s tests -p 'test_*.py'"],
            attempts=1,
            diagnostics=None,
        )


class _FailureRunner:
    def run(self, request):  # noqa: ANN001
        return WorkflowResult(
            succeeded=False,
            plan=PmPlan(
                plan_steps=["plan", "build", "validate"],
                acceptance_criteria=["has PR link"],
                risks=["tool outage"],
            ),
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=2,
            diagnostics=WorkflowDiagnostics(
                stage="test",
                message="Max workflow attempts reached after test failures",
                attempts=2,
                history=[{"stage": "test", "attempt": "2", "event": "failure"}],
            ),
        )


class WorkerWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_test.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._create_tenant()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _create_tenant(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-worker",
                    name="Tenant Worker",
                    is_enabled=True,
                    jira_config={
                        "mcp_endpoint": "https://mcp.example.test",
                        "project_keys": ["TP"],
                        "ready_label": "agent:ready",
                        "in_progress_label": "agent:in-progress",
                        "blocked_label": "agent:blocked",
                        "done_label": "agent:done",
                        "webhook_secret_ref": None,
                    },
                    github_config={
                        "mode": "github_app",
                        "app_id_ref": "secret/app-id",
                        "private_key_ref": "secret/private-key",
                        "webhook_secret_ref": None,
                        "installation_id": "12345",
                    },
                    repos_config={
                        "allowlist": ["https://github.com/example/repo"],
                        "mapping_rules_by_project_key": {"TP": "https://github.com/example/repo"},
                        "mapping_rules_by_component": {},
                        "fallback_repo": None,
                    },
                    policy_config={
                        "allow_jira_transitions": False,
                        "allow_pr_creation": True,
                        "allow_label_mutations": True,
                        "max_runtime_minutes": 30,
                        "max_dev_test_review_loops": 2,
                        "max_concurrent_runs": 2,
                        "allowed_commands": ["python3 -m unittest"],
                        "require_agents_md": False,
                    },
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def _queue_run(self, issue_key: str) -> str:
        now = datetime.now(timezone.utc)
        run_id = f"run-{issue_key}"
        with self.session_factory() as session:
            session.add(
                Run(
                    run_id=run_id,
                    tenant_id="tenant-worker",
                    issue_key=issue_key,
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    status="queued",
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                )
            )
            session.commit()
        return run_id

    def test_process_next_queued_run_marks_success_and_persists_plan(self) -> None:
        run_id = self._queue_run("TP-300")
        runner = _SuccessRunner()

        with self.session_factory() as session:
            processed = process_next_queued_run(session, runner)
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "succeeded")
            self.assertEqual(processed.pr_url, "https://github.com/example/repo/pull/99")
            self.assertIsNotNone(processed.started_at)
            self.assertIsNotNone(processed.finished_at)
            self.assertIsInstance(processed.plan, dict)
            self.assertTrue(processed.plan["succeeded"])
            self.assertEqual(processed.plan["attempts"], 1)
            self.assertIsNotNone(runner.last_request)
            self.assertIn("Good To Do checklist", runner.last_request.issue_description)
            self.assertIn("Decision Gate", runner.last_request.issue_description)

    def test_process_next_queued_run_marks_failure_with_diagnostics(self) -> None:
        run_id = self._queue_run("TP-301")

        with self.session_factory() as session:
            processed = process_next_queued_run(session, _FailureRunner())
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "failed")
            self.assertEqual(
                processed.last_error,
                "Max workflow attempts reached after test failures",
            )
            self.assertIsNone(processed.pr_url)
            self.assertIsInstance(processed.plan, dict)
            self.assertFalse(processed.plan["succeeded"])
            self.assertEqual(processed.plan["diagnostics"]["stage"], "test")
