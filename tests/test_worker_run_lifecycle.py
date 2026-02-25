import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from orchestrator.core.runs import enqueue_run
from orchestrator.core.worker.run_lifecycle import (
    bind_run_project,
    block_archived_project,
    fail_missing_project_mapping,
    fail_project_repository_checkout,
    finalize_workflow_result,
    requeue_workflow_result_for_capability,
    resolve_project_for_run,
    start_run,
)
from orchestrator.core.workflow.runner import PmPlan, WorkflowDiagnostics, WorkflowResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Run, RunLock, Tenant


class WorkerRunLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_lifecycle.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._seed_tenants_projects()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    def _seed_tenants_projects(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={"project_keys": ["TA"]},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={},
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
                    jira_config={"project_keys": ["TB"]},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/b"},
                    policy_config={},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="tenant-a-default",
                    tenant_id="tenant-a",
                    name="Default A",
                    github_repository="https://github.com/example/a",
                    jira_project_key="TA",
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="tenant-b-default",
                    tenant_id="tenant-b",
                    name="Default B",
                    github_repository="https://github.com/example/b",
                    jira_project_key="TB",
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_resolve_project_for_run_and_bind_run_project(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            run = Run(
                run_id="run-1",
                tenant_id="tenant-a",
                issue_key="TA-100",
                issue_summary="resolve project",
                issue_description="desc",
                repo_url=None,
                branch=None,
                pr_url=None,
                status="queued",
                last_error=None,
                plan=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                project_id="tenant-b-default",
            )
            session.add(run)
            session.commit()
            session.refresh(run)

            project = resolve_project_for_run(session, run=run)
            self.assertIsNotNone(project)
            self.assertEqual(project.project_id, "tenant-a-default")

            bind_run_project(session, run=run, project=project)
            self.assertEqual(run.project_id, "tenant-a-default")
            self.assertEqual(run.repo_url, "https://github.com/example/a")

    def test_fail_missing_project_mapping_releases_lock(self) -> None:
        with self.session_factory() as session:
            queued = enqueue_run(
                session,
                tenant_id="tenant-a",
                project_id=None,
                issue_key="ZZ-404",
                issue_summary="Missing mapping",
                issue_description=(
                    "Objective: test lifecycle failure path. "
                    "Acceptance Criteria: lock released. "
                    "How to test: fail missing project mapping."
                ),
                repo_url="https://github.com/example/a",
            )
            self.assertTrue(queued.enqueued)
            run = queued.run

            failed_run = fail_missing_project_mapping(session, run=run)
            self.assertEqual(failed_run.status, "failed")
            self.assertEqual(
                failed_run.last_error,
                "No active project mapping found for issue ZZ-404",
            )
            lock = session.get(RunLock, {"tenant_id": "tenant-a", "issue_key": "ZZ-404"})
            self.assertIsNone(lock)

    def test_fail_project_repository_checkout_releases_lock(self) -> None:
        with self.session_factory() as session:
            queued = enqueue_run(
                session,
                tenant_id="tenant-a",
                project_id=None,
                issue_key="TA-401",
                issue_summary="Checkout failed",
                issue_description=(
                    "Objective: fail checkout safely. "
                    "Acceptance Criteria: lock released. "
                    "How to test: fail repository checkout."
                ),
                repo_url="https://github.com/example/a",
            )
            self.assertTrue(queued.enqueued)
            run = queued.run

            failed_run = fail_project_repository_checkout(
                session,
                run=run,
                error="clone failed",
            )
            self.assertEqual(failed_run.status, "failed")
            self.assertEqual(
                failed_run.last_error,
                "Project repository checkout failed: clone failed",
            )
            lock = session.get(RunLock, {"tenant_id": "tenant-a", "issue_key": "TA-401"})
            self.assertIsNone(lock)

    def test_start_block_and_finalize_workflow_result(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            project = session.get(Project, "tenant-a-default")
            assert project is not None
            project.is_archived = True
            run = Run(
                run_id="run-2",
                tenant_id="tenant-a",
                issue_key="TA-200",
                issue_summary="lifecycle states",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                last_error=None,
                plan=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                project_id="tenant-a-default",
            )
            session.add(run)
            session.add(
                RunLock(
                    tenant_id="tenant-a",
                    issue_key="TA-200",
                    run_id="run-2",
                    locked_at=now,
                )
            )
            session.commit()
            session.refresh(run)

            start_run(session, run=run)
            self.assertEqual(run.status, "running")
            self.assertIsNotNone(run.started_at)

            blocked = block_archived_project(session, run=run, project=project)
            self.assertEqual(blocked.status, "blocked")
            self.assertIn("archived", blocked.last_error or "")
            self.assertIsNotNone(blocked.finished_at)

            # Reset to validate finalize helper independently.
            run.status = "running"
            run.last_error = None
            run.finished_at = None
            session.commit()
            session.refresh(run)

            result = WorkflowResult(
                succeeded=False,
                plan=PmPlan(
                    plan_steps=["a"],
                    acceptance_criteria=["b"],
                    risks=[],
                ),
                pr_url=None,
                summary=[],
                test_guidance=[],
                attempts=2,
                diagnostics=WorkflowDiagnostics(
                    stage="test",
                    message="failure details",
                    attempts=2,
                    history=[],
                ),
            )
            finalized = finalize_workflow_result(
                session,
                run=run,
                workflow_result=result,
                stage_updates=[{"stage": "run_failed"}],
            )
            self.assertEqual(finalized.status, "failed")
            self.assertEqual(finalized.last_error, "failure details")
            self.assertEqual(finalized.plan["stage_updates"], [{"stage": "run_failed"}])
            lock = session.get(RunLock, {"tenant_id": "tenant-a", "issue_key": "TA-200"})
            self.assertIsNone(lock)

    def test_start_run_returns_none_when_status_does_not_match_expected(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            run = Run(
                run_id="run-expected-status",
                tenant_id="tenant-a",
                issue_key="TA-201",
                issue_summary="claim guarded by expected status",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                last_error=None,
                plan=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                project_id="tenant-a-default",
            )
            session.add(run)
            session.commit()
            session.refresh(run)

            started = start_run(session, run=run, expected_status="queued")
            self.assertIsNone(started)
            refreshed = session.get(Run, "run-expected-status")
            assert refreshed is not None
            self.assertEqual(refreshed.status, "running")

    def test_requeue_workflow_result_for_capability_emits_queue_notification(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            run = Run(
                run_id="run-requeue-notify",
                tenant_id="tenant-a",
                issue_key="TA-202",
                issue_summary="capability requeue notification",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                last_error="previous error",
                plan=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                project_id="tenant-a-default",
            )
            session.add(run)
            session.add(
                RunLock(
                    tenant_id="tenant-a",
                    issue_key="TA-202",
                    run_id="run-requeue-notify",
                    locked_at=now,
                )
            )
            session.commit()
            session.refresh(run)

            workflow_result = WorkflowResult(
                succeeded=False,
                plan=PmPlan(
                    plan_steps=["plan"],
                    acceptance_criteria=["criterion"],
                    risks=["requires macos"],
                    execution_worker_capability="macos",
                ),
                pr_url=None,
                summary=[],
                test_guidance=[],
                attempts=1,
                diagnostics=WorkflowDiagnostics(
                    stage="pm",
                    message="Execution capability mismatch: selected macos",
                    attempts=1,
                    history=[],
                ),
            )

            with patch("orchestrator.core.worker.run_lifecycle.notify_run_enqueued") as notify_mock:
                requeued = requeue_workflow_result_for_capability(
                    session,
                    run=run,
                    workflow_result=workflow_result,
                    stage_updates=[{"stage": "run_requeued_capability_mismatch"}],
                    required_worker_capability="macos",
                    required_worker_label="worker:macos",
                )

            self.assertEqual(requeued.status, "queued")
            self.assertIsNone(requeued.started_at)
            self.assertIsNone(requeued.finished_at)
            self.assertTrue(requeued.plan["requeued"])
            self.assertEqual(requeued.plan["required_worker_capability"], "macos")
            notify_mock.assert_called_once_with(
                session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-requeue-notify",
                issue_key="TA-202",
            )
            lock = session.get(RunLock, {"tenant_id": "tenant-a", "issue_key": "TA-202"})
            self.assertIsNone(lock)
