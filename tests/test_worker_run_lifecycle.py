import os
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from orchestrator.core.runs import RUN_DEDUPE_SCOPE_ISSUE_EXECUTION, enqueue_run
from orchestrator.core.worker_capability_normalization import WorkerCapability
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
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
from orchestrator.storage.models import Project, Run, Tenant, WorkflowExecution
from tests.test_support.db_harness import SqliteTemplateDbTestCase
from tests.workflow_test_support import add_workflow_attempt


class WorkerRunLifecycleTests(SqliteTemplateDbTestCase):
    @classmethod
    def bootstrap_template_database(cls) -> None:
        session_factory = create_session_factory(database_url=cls._template_database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
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

    def setUp(self) -> None:
        self._original_database_url = os.environ.get("ORCHESTRATOR_DATABASE_URL")
        self.database_url = self._prepare_test_database(name_prefix="worker-lifecycle")
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        self.session_factory = create_session_factory(database_url=self.database_url)

    def tearDown(self) -> None:
        self._cleanup_test_database()
        if self._original_database_url is None:
            os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        else:
            os.environ["ORCHESTRATOR_DATABASE_URL"] = self._original_database_url
        reset_db_engine_cache()

    def _get_workflow(self, session, *, issue_key: str, dedupe_scope: str = RUN_DEDUPE_SCOPE_ISSUE_EXECUTION):
        return session.query(WorkflowExecution).filter_by(
            tenant_id="tenant-a",
            issue_key=issue_key,
            dedupe_scope=dedupe_scope,
        ).one_or_none()

    @staticmethod
    def _canonical_plan(
        *,
        trigger_context: dict | None = None,
        execution_context: dict | None = None,
        live_stage_updates: list[dict] | None = None,
    ) -> dict:
        snapshot = ExecutionSnapshot.empty(trigger_context=trigger_context)
        if execution_context:
            snapshot.context.execution_context = dict(execution_context)
        if live_stage_updates:
            snapshot.events.live_stage_updates = [dict(item) for item in live_stage_updates]
        return snapshot.dump()

    def test_resolve_project_for_run_and_bind_run_project(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-1",
                tenant_id="tenant-a",
                project_id="tenant-b-default",
                issue_key="TA-100",
                issue_summary="resolve project",
                issue_description="desc",
                repo_url=None,
                created_at=now,
            )
            session.commit()
            session.refresh(run)

            project = resolve_project_for_run(session, run=run)
            self.assertIsNotNone(project)
            self.assertEqual(project.project_id, "tenant-a-default")

            bind_run_project(session, run=run, project=project)
            self.assertEqual(run.project_id, "tenant-a-default")
            self.assertEqual(run.repo_url, "https://github.com/example/a")

    def test_fail_missing_project_mapping_updates_workflow(self) -> None:
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
            workflow = self._get_workflow(session, issue_key="ZZ-404")
            assert workflow is not None
            self.assertEqual(workflow.status, "failed")

    def test_fail_project_repository_checkout_updates_workflow(self) -> None:
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
            workflow = self._get_workflow(session, issue_key="TA-401")
            assert workflow is not None
            self.assertEqual(workflow.status, "failed")

    def test_start_block_and_finalize_workflow_result(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            project = session.get(Project, "tenant-a-default")
            assert project is not None
            project.is_archived = True
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-2",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-200",
                issue_summary="lifecycle states",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                created_at=now,
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
            run.plan = self._canonical_plan(
                trigger_context={"source": "github_pr_review_feedback", "pr_number": 6}
            )
            session.commit()
            session.refresh(run)

            result = WorkflowResult(
                outcome="failed",
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
            self.assertEqual(finalized.plan["events"]["stage_updates"], [{"stage": "run_failed"}])
            self.assertEqual(
                finalized.plan["context"]["trigger_context"],
                {"source": "github_pr_review_feedback", "pr_number": 6},
            )
            workflow = self._get_workflow(session, issue_key="TA-200")
            assert workflow is not None
            self.assertEqual(workflow.status, "failed")

    def test_persist_stage_checkpoint_merges_artifacts_and_survives_finalization(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-checkpoint",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-205",
                issue_summary="persist checkpoints",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                plan=self._canonical_plan(
                    trigger_context={"source": "manual"},
                    live_stage_updates=[{"stage": "lock_acquired", "recorded_at": now.isoformat()}],
                ),
                workflow_status="running",
                run_status="running",
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id="node-a:1234",
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
                execution_context={
                    "execution_branch": "run/ta-205/run-checkpoint",
                    "integration_branch": "feature/TA-205",
                },
                expected_worker_service_instance_id="node-a:1234",
            )

            self.assertEqual(run.plan["context"]["trigger_context"], {"source": "manual"})
            self.assertEqual(run.plan["stages"]["pm"]["artifact"]["plan_steps"], ["plan"])
            self.assertEqual(run.plan["stages"]["pm"]["status"], "completed")
            self.assertEqual(
                run.plan["context"]["execution_context"]["execution_branch"],
                "run/ta-205/run-checkpoint",
            )
            self.assertEqual(
                run.plan["context"]["execution_context"]["integration_branch"],
                "feature/TA-205",
            )
            self.assertEqual(run.plan["events"]["live_stage_updates"][0]["stage"], "lock_acquired")

            finalized = finalize_workflow_result(
                session,
                run=run,
                workflow_result=WorkflowResult(
                    outcome="success",
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
            self.assertEqual(finalized.plan["stages"]["pm"]["status"], "completed")
            self.assertEqual(finalized.plan["context"]["trigger_context"], {"source": "manual"})

    def test_start_run_returns_none_when_status_does_not_match_expected(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-expected-status",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-201",
                issue_summary="claim guarded by expected status",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                workflow_status="running",
                run_status="running",
                created_at=now,
                started_at=now,
            )
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
            add_workflow_attempt(
                session,
                run_id="run-already-running",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-301",
                issue_summary="already running",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                workflow_status="running",
                run_status="running",
                created_at=now,
                started_at=now,
            )
            _, queued, _ = add_workflow_attempt(
                session,
                run_id="run-queued-under-limit",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-302",
                issue_summary="queued candidate",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                created_at=now,
            )
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

    def test_finalize_workflow_result_fails_on_unsupported_outcome(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-unsupported-outcome",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-998",
                issue_summary="unsupported outcome",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                workflow_status="running",
                run_status="running",
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id="node-a:1234",
            )
            session.commit()
            session.refresh(run)

            unsupported_result = WorkflowResult(
                outcome="success",  # typed baseline; overridden below for unsupported-path coverage
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
            object.__setattr__(unsupported_result, "outcome", "nonsense")

            finalized = finalize_workflow_result(
                session,
                run=run,
                workflow_result=unsupported_result,
                stage_updates=[{"stage": "task_completed"}],
                expected_worker_service_instance_id="node-a:1234",
            )
            self.assertEqual(finalized.status, "failed")
            self.assertIn("Unsupported workflow outcome", finalized.last_error or "")

    def test_finalize_workflow_result_returns_run_when_ownership_is_lost(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-ownership-lost",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-999",
                issue_summary="ownership lost",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                workflow_status="running",
                run_status="running",
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id="node-b:9999",
            )
            session.commit()
            session.refresh(run)

            workflow_result = WorkflowResult(
                outcome="success",
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
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-capability-requeue",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-202",
                issue_summary="capability requeue",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                workflow_status="running",
                run_status="running",
                last_error="old error",
                plan=self._canonical_plan(
                    trigger_context={"source": "github_pr_review_feedback", "pr_number": 6}
                ),
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id="node-a:1234",
            )
            session.commit()
            session.refresh(run)

            workflow_result = WorkflowResult(
                outcome="requeue",
                plan=PmPlan(
                    plan_steps=["retry on required capability"],
                    acceptance_criteria=["run is queued for a compatible worker"],
                    risks=[],
                ),
                pr_url=None,
                summary=[],
                test_guidance=[],
                attempts=1,
                requeue_target=WorkerCapability.MACOS,
                requeue_reason="Execution capability mismatch: PM selected macos but current worker is linux.",
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
            self.assertEqual(requeued.required_worker_capability, "macos")
            self.assertEqual(requeued.plan["workflow"]["requeue_target"], "macos")
            self.assertEqual(requeued.plan["context"]["execution_context"]["required_worker_label"], "macos")
            self.assertEqual(
                requeued.plan["context"]["trigger_context"],
                {"source": "github_pr_review_feedback", "pr_number": 6},
            )
            notify_mock.assert_called_once_with(
                session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-capability-requeue",
                issue_key="TA-202",
            )
            workflow = self._get_workflow(session, issue_key="TA-202")
            assert workflow is not None
            self.assertEqual(workflow.status, "queued")

    def test_requeue_workflow_result_for_stale_snapshot_notifies_queue_listener(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            _, run, _ = add_workflow_attempt(
                session,
                run_id="run-stale-requeue",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TA-203",
                issue_summary="stale snapshot requeue",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch="feature/TA-203",
                pr_url="https://github.com/example/a/pull/88",
                workflow_status="running",
                run_status="running",
                last_error="old error",
                plan=self._canonical_plan(
                    trigger_context={"source": "github_pr_review_feedback", "pr_number": 6}
                ),
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id="node-a:1234",
            )
            session.commit()
            session.refresh(run)

            workflow_result = WorkflowResult(
                outcome="success",
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
            self.assertTrue(requeued.plan["context"]["execution_context"]["stale_branch_snapshot"])
            self.assertEqual(requeued.plan["workflow"]["outcome"], "requeue")
            self.assertIsNone(requeued.plan["workflow"]["requeue_target"])
            self.assertIn("Branch snapshot stale", requeued.plan["workflow"]["requeue_reason"])
            self.assertIsNotNone(ExecutionSnapshot.load(requeued.plan))
            self.assertEqual(
                requeued.plan["context"]["trigger_context"],
                {"source": "github_pr_review_feedback", "pr_number": 6},
            )
            notify_mock.assert_called_once_with(
                session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-stale-requeue",
                issue_key="TA-203",
            )
            workflow = self._get_workflow(session, issue_key="TA-203")
            assert workflow is not None
            self.assertEqual(workflow.status, "queued")
