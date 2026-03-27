import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from orchestrator.core.runs import RUN_DEDUPE_SCOPE_ISSUE_EXECUTION, enqueue_run
from orchestrator.core.worker.run_lifecycle import (
    bind_run_project,
    block_archived_project,
    fail_missing_project_mapping,
    fail_project_repository_checkout,
    finalize_workflow_result,
    persist_stage_checkpoint,
    requeue_workflow_result_for_capability,
    requeue_workflow_result_for_stale_snapshot,
    resolve_project_for_run,
    start_run,
)
from orchestrator.core.workflow.runner import (
    PmPlan,
    WorkflowDiagnostics,
    WorkflowResult,
    WorkflowStageCheckpoint,
)
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

    def _get_lock(self, session, *, issue_key: str, dedupe_scope: str = RUN_DEDUPE_SCOPE_ISSUE_EXECUTION):
        return session.get(
            RunLock,
            {
                "tenant_id": "tenant-a",
                "issue_key": issue_key,
                "dedupe_scope": dedupe_scope,
            },
        )

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
            lock = self._get_lock(session, issue_key="ZZ-404")
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
            lock = self._get_lock(session, issue_key="TA-401")
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
                    dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                    run_id="run-2",
                    locked_at=now,
                )
            )
            session.commit()
            session.refresh(run)

            start_run(session, run=run, worker_service_instance_id="node-a:1234")
            self.assertEqual(run.status, "running")
            self.assertIsNotNone(run.started_at)
            self.assertEqual(run.worker_service_instance_id, "node-a:1234")
            self.assertEqual(run.last_heartbeat_at, run.started_at)

            blocked = block_archived_project(session, run=run, project=project)
            self.assertEqual(blocked.status, "blocked")
            self.assertIn("archived", blocked.last_error or "")
            self.assertIsNotNone(blocked.finished_at)

            # Reset to validate finalize helper independently.
            run.status = "running"
            run.last_error = None
            run.finished_at = None
            run.plan = {"trigger_context": {"source": "github_pr_review_feedback", "pr_number": 6}}
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
            self.assertEqual(
                finalized.plan.get("trigger_context"),
                {"source": "github_pr_review_feedback", "pr_number": 6},
            )
            lock = self._get_lock(session, issue_key="TA-200")
            self.assertIsNone(lock)

    def test_persist_stage_checkpoint_merges_artifacts_and_survives_finalization(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            run = Run(
                run_id="run-checkpoint",
                tenant_id="tenant-a",
                issue_key="TA-205",
                issue_summary="persist checkpoints",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                last_error=None,
                plan={
                    "trigger_context": {"resume_stage": "dev"},
                    "live_stage_updates": [{"stage": "lock_acquired", "recorded_at": now.isoformat()}],
                },
                created_at=now,
                started_at=now,
                finished_at=None,
                project_id="tenant-a-default",
                worker_service_instance_id="node-a:1234",
            )
            session.add(run)
            session.add(
                RunLock(
                    tenant_id="tenant-a",
                    issue_key="TA-205",
                    dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                    run_id="run-checkpoint",
                    locked_at=now,
                )
            )
            session.commit()
            session.refresh(run)

            persist_stage_checkpoint(
                session,
                run=run,
                checkpoint=WorkflowStageCheckpoint(
                    stage="pm",
                    attempt=1,
                    status="completed",
                    summary="PM completed",
                    plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac"], risks=[]),
                ),
                expected_worker_service_instance_id="node-a:1234",
            )

            self.assertEqual(run.plan["trigger_context"], {"resume_stage": "dev"})
            self.assertEqual(run.plan["plan"]["plan_steps"], ["plan"])
            self.assertEqual(run.plan["stage_checkpoints"]["pm"]["status"], "completed")
            self.assertEqual(run.plan["live_stage_updates"][0]["stage"], "lock_acquired")

            finalized = finalize_workflow_result(
                session,
                run=run,
                workflow_result=WorkflowResult(
                    succeeded=True,
                    plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac"], risks=[]),
                    pr_url=None,
                    summary=["done"],
                    test_guidance=["pytest -q"],
                    attempts=1,
                ),
                stage_updates=[{"stage": "task_completed"}],
                expected_worker_service_instance_id="node-a:1234",
            )

            self.assertEqual(finalized.status, "succeeded")
            self.assertEqual(finalized.plan["stage_checkpoints"]["pm"]["status"], "completed")
            self.assertEqual(finalized.plan["trigger_context"], {"resume_stage": "dev"})

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

    def test_start_run_only_guards_expected_status(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            running = Run(
                run_id="run-already-running",
                tenant_id="tenant-a",
                issue_key="TA-301",
                issue_summary="already running",
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
            queued = Run(
                run_id="run-queued-under-limit",
                tenant_id="tenant-a",
                issue_key="TA-302",
                issue_summary="queued candidate",
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
            session.add_all([running, queued])
            session.commit()
            session.refresh(queued)

            started = start_run(
                session,
                run=queued,
                expected_status="queued",
                worker_service_instance_id="node-a:1234",
            )
            self.assertIsNotNone(started)
            assert started is not None
            self.assertEqual(started.status, "running")
            self.assertEqual(started.worker_service_instance_id, "node-a:1234")
            self.assertIsNotNone(started.last_heartbeat_at)
            refreshed_running = session.get(Run, "run-already-running")
            assert refreshed_running is not None
            self.assertEqual(refreshed_running.status, "running")

    def test_finalize_workflow_result_returns_run_when_ownership_is_lost(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            run = Run(
                run_id="run-ownership-lost",
                tenant_id="tenant-a",
                issue_key="TA-999",
                issue_summary="ownership lost",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                last_error=None,
                plan=None,
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                finished_at=None,
                project_id="tenant-a-default",
                worker_service_instance_id="node-b:9999",
            )
            session.add(run)
            session.commit()
            session.refresh(run)

            workflow_result = WorkflowResult(
                succeeded=True,
                plan=PmPlan(
                    plan_steps=["done"],
                    acceptance_criteria=["done"],
                    risks=[],
                ),
                pr_url=None,
                summary=[],
                test_guidance=[],
                attempts=1,
            )
            finalized = finalize_workflow_result(
                session,
                run=run,
                workflow_result=workflow_result,
                stage_updates=[{"stage": "task_completed"}],
                expected_worker_service_instance_id="node-a:1234",
            )
            self.assertEqual(finalized.status, "running")
            self.assertIsNone(finalized.finished_at)
            self.assertEqual(finalized.worker_service_instance_id, "node-b:9999")

    def test_requeue_workflow_result_for_capability_notifies_queue_listener(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            run = Run(
                run_id="run-capability-requeue",
                tenant_id="tenant-a",
                issue_key="TA-202",
                issue_summary="capability requeue",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                last_error="old error",
                plan={"trigger_context": {"source": "github_pr_review_feedback", "pr_number": 6}},
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                finished_at=None,
                project_id="tenant-a-default",
                worker_service_instance_id="node-a:1234",
            )
            session.add(run)
            session.add(
                RunLock(
                    tenant_id="tenant-a",
                    issue_key="TA-202",
                    dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                    run_id="run-capability-requeue",
                    locked_at=now,
                )
            )
            session.commit()
            session.refresh(run)

            workflow_result = WorkflowResult(
                succeeded=False,
                plan=PmPlan(
                    plan_steps=["retry on required capability"],
                    acceptance_criteria=["run is queued for a compatible worker"],
                    risks=[],
                ),
                pr_url=None,
                summary=[],
                test_guidance=[],
                attempts=1,
                diagnostics=WorkflowDiagnostics(
                    stage="dev",
                    message="Execution capability mismatch: PM selected macos but current worker is linux.",
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
                    required_worker_label="macos",
                )

            self.assertEqual(requeued.status, "queued")
            self.assertIsNone(requeued.last_error)
            self.assertIsNone(requeued.started_at)
            self.assertIsNone(requeued.last_heartbeat_at)
            self.assertIsNone(requeued.finished_at)
            self.assertIsNone(requeued.worker_service_instance_id)
            self.assertEqual(requeued.plan["required_worker_capability"], "macos")
            self.assertEqual(requeued.plan["required_worker_label"], "macos")
            self.assertTrue(requeued.plan["requeued"])
            self.assertEqual(
                requeued.plan.get("trigger_context"),
                {"source": "github_pr_review_feedback", "pr_number": 6},
            )
            notify_mock.assert_called_once_with(
                session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-capability-requeue",
                issue_key="TA-202",
            )
            lock = self._get_lock(session, issue_key="TA-202")
            self.assertIsNone(lock)

    def test_requeue_workflow_result_for_stale_snapshot_notifies_queue_listener(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            run = Run(
                run_id="run-stale-requeue",
                tenant_id="tenant-a",
                issue_key="TA-203",
                issue_summary="stale snapshot requeue",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch="feature/TA-203",
                pr_url="https://github.com/example/a/pull/88",
                status="running",
                last_error="old error",
                plan={"trigger_context": {"source": "github_pr_review_feedback", "pr_number": 6}},
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                finished_at=None,
                project_id="tenant-a-default",
                worker_service_instance_id="node-a:1234",
            )
            session.add(run)
            session.add(
                RunLock(
                    tenant_id="tenant-a",
                    issue_key="TA-203",
                    dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                    run_id="run-stale-requeue",
                    locked_at=now,
                )
            )
            session.commit()
            session.refresh(run)

            workflow_result = WorkflowResult(
                succeeded=True,
                plan=PmPlan(
                    plan_steps=["finalize"],
                    acceptance_criteria=["PR exists"],
                    risks=[],
                ),
                pr_url="https://github.com/example/a/pull/88",
                summary=["complete"],
                test_guidance=["pytest"],
                attempts=1,
                diagnostics=None,
            )

            with patch("orchestrator.core.worker.run_lifecycle.notify_run_enqueued") as notify_mock:
                requeued = requeue_workflow_result_for_stale_snapshot(
                    session,
                    run=run,
                    workflow_result=workflow_result,
                    stage_updates=[{"stage": "run_requeued_stale_snapshot"}],
                    error="Branch snapshot stale: origin/main moved from aaa to bbb.",
                )

            self.assertEqual(requeued.status, "queued")
            self.assertIsNone(requeued.last_error)
            self.assertIsNone(requeued.started_at)
            self.assertIsNone(requeued.last_heartbeat_at)
            self.assertIsNone(requeued.finished_at)
            self.assertIsNone(requeued.pr_url)
            self.assertIsNone(requeued.worker_service_instance_id)
            self.assertTrue(requeued.plan["requeued"])
            self.assertTrue(requeued.plan["stale_branch_snapshot"])
            self.assertIn("Branch snapshot stale", requeued.plan["requeue_reason"])
            self.assertEqual(
                requeued.plan.get("trigger_context"),
                {"source": "github_pr_review_feedback", "pr_number": 6},
            )
            notify_mock.assert_called_once_with(
                session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-stale-requeue",
                issue_key="TA-203",
            )
            lock = self._get_lock(session, issue_key="TA-203")
            self.assertIsNone(lock)
