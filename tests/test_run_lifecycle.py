import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from orchestrator.core.config import get_settings
from orchestrator.core.runs import (
    RUN_STATUS_BLOCKED,
    RUN_STATUS_SUCCEEDED,
    RunStateTransitionError,
    enqueue_run,
    mark_run_running,
    mark_run_terminal,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import RunLock, Tenant


class RunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/runs_test.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._create_tenant("tenant-runs")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _create_tenant(self, tenant_id: str) -> None:
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    name="Tenant Runs",
                    is_enabled=True,
                    jira_config={
                        "mcp_endpoint": "https://mcp.example.test",
                        "auth_ref": "secret/jira",
                        "project_keys": ["TP"],
                        "ready_label": "agent:ready",
                        "in_progress_label": "agent:in-progress",
                        "blocked_label": "agent:blocked",
                        "done_label": "agent:done",
                        "ready_jql": "project = TP",
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
                        "allowed_commands": [],
                        "require_agents_md": False,
                    },
                    discord_config=None,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

    def test_enqueue_is_idempotent_for_active_issue(self) -> None:
        with self.session_factory() as session:
            first = enqueue_run(session, tenant_id="tenant-runs", issue_key="TP-901")
            second = enqueue_run(session, tenant_id="tenant-runs", issue_key="TP-901")

            self.assertTrue(first.enqueued)
            self.assertFalse(second.enqueued)
            self.assertEqual(second.reason, "run_already_active")
            self.assertEqual(second.run.run_id, first.run.run_id)

            lock = session.get(RunLock, {"tenant_id": "tenant-runs", "issue_key": "TP-901"})
            self.assertIsNotNone(lock)
            self.assertEqual(lock.run_id, first.run.run_id)

    def test_enqueue_deduplicates_delivery_identifier(self) -> None:
        with self.session_factory() as session:
            first = enqueue_run(
                session,
                tenant_id="tenant-runs",
                issue_key="TP-902",
                delivery_id="delivery-xyz",
            )
            second = enqueue_run(
                session,
                tenant_id="tenant-runs",
                issue_key="TP-902",
                delivery_id="delivery-xyz",
            )

            self.assertTrue(first.enqueued)
            self.assertFalse(second.enqueued)
            self.assertEqual(second.reason, "duplicate_delivery")
            self.assertEqual(second.run.run_id, first.run.run_id)

    def test_running_to_success_releases_lock_and_persists_timestamps(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(session, tenant_id="tenant-runs", issue_key="TP-903")
            running = mark_run_running(session, run_id=enqueue.run.run_id)
            self.assertEqual(running.status, "running")
            self.assertIsNotNone(running.started_at)

            completed = mark_run_terminal(
                session,
                run_id=enqueue.run.run_id,
                terminal_status=RUN_STATUS_SUCCEEDED,
            )

            self.assertEqual(completed.status, RUN_STATUS_SUCCEEDED)
            self.assertIsNotNone(completed.started_at)
            self.assertIsNotNone(completed.finished_at)

            lock = session.get(RunLock, {"tenant_id": "tenant-runs", "issue_key": "TP-903"})
            self.assertIsNone(lock)

    def test_failure_path_marks_blocked_and_cleans_up_lock(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(session, tenant_id="tenant-runs", issue_key="TP-904")
            mark_run_running(session, run_id=enqueue.run.run_id)
            blocked = mark_run_terminal(
                session,
                run_id=enqueue.run.run_id,
                terminal_status=RUN_STATUS_BLOCKED,
                last_error="jira label mutation failed",
            )

            self.assertEqual(blocked.status, RUN_STATUS_BLOCKED)
            self.assertEqual(blocked.last_error, "jira label mutation failed")
            self.assertIsNotNone(blocked.finished_at)

            lock = session.get(RunLock, {"tenant_id": "tenant-runs", "issue_key": "TP-904"})
            self.assertIsNone(lock)

    def test_invalid_state_transition_is_rejected(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(session, tenant_id="tenant-runs", issue_key="TP-905")
            mark_run_terminal(
                session,
                run_id=enqueue.run.run_id,
                terminal_status=RUN_STATUS_SUCCEEDED,
            )

            with self.assertRaises(RunStateTransitionError):
                mark_run_running(session, run_id=enqueue.run.run_id)
