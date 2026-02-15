import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from orchestrator.core.worker.queue_selector import (
    coerce_positive_int,
    select_next_queued_run,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Run, Tenant


class WorkerQueueSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_queue_selector.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    def test_coerce_positive_int_handles_invalid_and_non_positive(self) -> None:
        self.assertEqual(coerce_positive_int(None, default=2), 2)
        self.assertEqual(coerce_positive_int("bad", default=2), 2)
        self.assertEqual(coerce_positive_int(0, default=2), 1)
        self.assertEqual(coerce_positive_int(-5, default=2), 1)
        self.assertEqual(coerce_positive_int("3", default=2), 3)

    def test_select_next_queued_run_marks_missing_tenant_terminal(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Run(
                    run_id="run-missing-tenant",
                    tenant_id="tenant-missing",
                    issue_key="MAB-900",
                    issue_summary="Missing tenant",
                    issue_description="How to test: verify missing tenant handling.",
                    repo_url="https://github.com/example/missing",
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

            result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
            )

            self.assertIsNotNone(result.terminal_run)
            self.assertIsNone(result.run)
            self.assertEqual(result.terminal_run.status, "failed")
            self.assertEqual(result.terminal_run.last_error, "Tenant not found for queued run")
            self.assertIsNotNone(result.terminal_run.finished_at)

    def test_select_next_queued_run_skips_concurrency_limited_tenant(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Tenant(
                    tenant_id="tenant-b",
                    name="Tenant B",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/b"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="run-a-running",
                    tenant_id="tenant-a",
                    issue_key="MAB-901",
                    issue_summary="Tenant A running",
                    issue_description="running",
                    repo_url="https://github.com/example/a",
                    branch=None,
                    pr_url=None,
                    status="running",
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=now,
                    finished_at=None,
                )
            )
            session.add(
                Run(
                    run_id="run-a-queued",
                    tenant_id="tenant-a",
                    issue_key="MAB-902",
                    issue_summary="Tenant A queued",
                    issue_description="queued",
                    repo_url="https://github.com/example/a",
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
            session.add(
                Run(
                    run_id="run-b-queued",
                    tenant_id="tenant-b",
                    issue_key="MAB-903",
                    issue_summary="Tenant B queued",
                    issue_description="queued",
                    repo_url="https://github.com/example/b",
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

            result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
            )

            self.assertIsNone(result.terminal_run)
            self.assertIsNotNone(result.run)
            self.assertIsNotNone(result.tenant)
            self.assertEqual(result.run.run_id, "run-b-queued")
            self.assertEqual(result.tenant.tenant_id, "tenant-b")

    def test_select_next_queued_run_applies_project_overrides_when_project_id_unset(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 5},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Tenant(
                    tenant_id="tenant-b",
                    name="Tenant B",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/b"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="tenant-a-proj",
                    tenant_id="tenant-a",
                    name="Tenant A Project",
                    github_repository="https://github.com/example/a",
                    jira_project_key="MAB",
                    policy_overrides={"max_concurrent_runs": 1},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="run-a-running",
                    tenant_id="tenant-a",
                    issue_key="MAB-100",
                    issue_summary="Tenant A running",
                    issue_description="running",
                    repo_url="https://github.com/example/a",
                    branch=None,
                    pr_url=None,
                    status="running",
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=now,
                    finished_at=None,
                )
            )
            session.add(
                Run(
                    run_id="run-a-queued-unbound",
                    tenant_id="tenant-a",
                    project_id=None,
                    issue_key="MAB-101",
                    issue_summary="Tenant A queued",
                    issue_description="queued",
                    repo_url="https://github.com/example/a",
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
            session.add(
                Run(
                    run_id="run-b-queued",
                    tenant_id="tenant-b",
                    issue_key="XYZ-1",
                    issue_summary="Tenant B queued",
                    issue_description="queued",
                    repo_url="https://github.com/example/b",
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

            result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
            )

            self.assertIsNone(result.terminal_run)
            self.assertIsNotNone(result.run)
            self.assertEqual(result.run.run_id, "run-b-queued")

    def test_select_next_queued_run_skips_incompatible_worker_capabilities(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-capabilities",
                    name="Tenant Capabilities",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/mobile"},
                    policy_config={"max_concurrent_runs": 2},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="run-macos",
                    tenant_id="tenant-capabilities",
                    issue_key="IOS-1",
                    issue_summary="Build iOS app with SwiftUI",
                    issue_description="Implement iOS app shell",
                    repo_url="https://github.com/example/mobile",
                    branch=None,
                    pr_url=None,
                    status="queued",
                    last_error=None,
                    plan={"required_worker_capability": "macos"},
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                )
            )
            session.add(
                Run(
                    run_id="run-linux",
                    tenant_id="tenant-capabilities",
                    issue_key="LINUX-1",
                    issue_summary="Build backend service",
                    issue_description="Implement API endpoint",
                    repo_url="https://github.com/example/backend",
                    branch=None,
                    pr_url=None,
                    status="queued",
                    last_error=None,
                    plan={"required_worker_capability": "linux"},
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                )
            )
            session.commit()

            linux_result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
                worker_capabilities={"linux"},
            )
            self.assertIsNotNone(linux_result.run)
            self.assertEqual(linux_result.run.run_id, "run-linux")

            macos_result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
                worker_capabilities={"macos"},
            )
            self.assertIsNotNone(macos_result.run)
            self.assertEqual(macos_result.run.run_id, "run-macos")
