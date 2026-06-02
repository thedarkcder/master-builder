import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from orchestrator.core.config import get_settings
from orchestrator.core.runs.service import (
    RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
    RUN_DEDUPE_SCOPE_PR_REMEDIATION,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_FAILED,
    RUN_STATUS_SUCCEEDED,
    RunBootstrap,
    RunStateTransitionError,
    enqueue_attempt_for_workflow_uncommitted,
    enqueue_run,
    mark_run_running,
    mark_run_terminal,
)
from orchestrator.core.worker.run_lifecycle import finalize_workflow_result, persist_stage_checkpoint
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import QaRecording, QaResult, QaScenario, WorkflowResult, WorkflowStageCheckpoint
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Run, Tenant, WorkflowCheckpoint, WorkflowExecution


class RunLifecycleTests(unittest.TestCase):
    _WORKER_ID = "worker-macos-local:runs"
    _CLAIM_ID = "claim-test"

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
                        "github_repository": "https://github.com/example/repo",
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

    def _get_workflow(self, session, *, issue_key: str, dedupe_scope: str = RUN_DEDUPE_SCOPE_ISSUE_EXECUTION):
        return session.query(WorkflowExecution).filter_by(
            tenant_id="tenant-runs",
            source_system="jira",
            source_ref=issue_key,
            dedupe_scope=dedupe_scope,
        ).one_or_none()

    def test_enqueue_is_idempotent_for_active_issue(self) -> None:
        with self.session_factory() as session:
            first = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-901",
                precheck_outcome="ready_for_agent",
            )
            second = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-901",
                precheck_outcome="ready_for_agent",
            )

            self.assertTrue(first.enqueued)
            self.assertFalse(second.enqueued)
            self.assertEqual(second.reason, "run_already_active")
            self.assertEqual(second.run.run_id, first.run.run_id)

            workflow = self._get_workflow(session, issue_key="TP-901")
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.active_run_id, first.run.run_id)
            self.assertEqual(workflow.status, "queued")

    def test_enqueue_allows_parallel_pr_remediation_and_issue_execution(self) -> None:
        with self.session_factory() as session:
            issue_run = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-907",
                dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                precheck_outcome="ready_for_agent",
            )
            remediation_run = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-907",
                dedupe_scope=RUN_DEDUPE_SCOPE_PR_REMEDIATION,
                precheck_outcome="ready_for_agent",
            )

            self.assertTrue(issue_run.enqueued)
            self.assertTrue(remediation_run.enqueued)
            self.assertNotEqual(issue_run.run.run_id, remediation_run.run.run_id)
            workflow = self._get_workflow(session, issue_key="TP-907")
            remediation_workflow = self._get_workflow(
                session,
                issue_key="TP-907",
                dedupe_scope=RUN_DEDUPE_SCOPE_PR_REMEDIATION,
            )
            assert workflow is not None
            assert remediation_workflow is not None
            self.assertEqual(workflow.active_run_id, issue_run.run.run_id)
            self.assertEqual(remediation_workflow.active_run_id, remediation_run.run.run_id)

    def test_enqueue_deduplicates_delivery_identifier(self) -> None:
        with self.session_factory() as session:
            first = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-902",
                delivery_id="delivery-xyz",
                precheck_outcome="ready_for_agent",
            )
            second = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-902",
                delivery_id="delivery-xyz",
                precheck_outcome="ready_for_agent",
            )

            self.assertTrue(first.enqueued)
            self.assertFalse(second.enqueued)
            self.assertEqual(second.reason, "duplicate_delivery")
            self.assertEqual(second.run.run_id, first.run.run_id)

    def test_enqueue_respects_tenant_concurrency_limit(self) -> None:
        with self.session_factory() as session:
            first = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-910",
                max_concurrent_runs=1,
                precheck_outcome="ready_for_agent",
            )
            second = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-911",
                max_concurrent_runs=1,
                precheck_outcome="ready_for_agent",
            )

            self.assertTrue(first.enqueued)
            self.assertFalse(second.enqueued)
            self.assertEqual(second.reason, "tenant_concurrency_limit_reached")
            self.assertEqual(second.run.run_id, first.run.run_id)

    def test_running_to_success_updates_workflow_and_persists_timestamps(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-903",
                precheck_outcome="ready_for_agent",
            )
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

            workflow = self._get_workflow(session, issue_key="TP-903")
            assert workflow is not None
            self.assertEqual(workflow.status, RUN_STATUS_SUCCEEDED)
            self.assertEqual(workflow.active_run_id, enqueue.run.run_id)
            self.assertIsNotNone(workflow.finished_at)

    def test_failure_path_marks_failed_workflow_and_persists_error(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-904",
                precheck_outcome="ready_for_agent",
            )
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

            workflow = self._get_workflow(session, issue_key="TP-904")
            assert workflow is not None
            self.assertEqual(workflow.status, RUN_STATUS_FAILED)
            self.assertEqual(workflow.last_error, "jira label mutation failed")
            self.assertIsNotNone(workflow.finished_at)

    def test_invalid_state_transition_is_rejected(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-905",
                precheck_outcome="ready_for_agent",
            )
            mark_run_terminal(
                session,
                run_id=enqueue.run.run_id,
                terminal_status=RUN_STATUS_SUCCEEDED,
            )

            with self.assertRaises(RunStateTransitionError):
                mark_run_running(session, run_id=enqueue.run.run_id)

    def test_worker_terminal_transition_requires_owner_and_claim(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-912",
                precheck_outcome="ready_for_agent",
            )
            run = mark_run_running(session, run_id=enqueue.run.run_id)
            run.worker_service_instance_id = "worker-a"
            run.claim_id = "claim-a"
            session.commit()

            with self.assertRaises(RunStateTransitionError):
                mark_run_terminal(
                    session,
                    run_id=enqueue.run.run_id,
                    terminal_status=RUN_STATUS_FAILED,
                    expected_worker_service_instance_id="worker-b",
                    expected_claim_id="claim-a",
                )
            with self.assertRaises(RunStateTransitionError):
                mark_run_terminal(
                    session,
                    run_id=enqueue.run.run_id,
                    terminal_status=RUN_STATUS_FAILED,
                    expected_worker_service_instance_id="worker-a",
                    expected_claim_id="claim-b",
                )

            completed = mark_run_terminal(
                session,
                run_id=enqueue.run.run_id,
                terminal_status=RUN_STATUS_SUCCEEDED,
                expected_worker_service_instance_id="worker-a",
                expected_claim_id="claim-a",
            )
            self.assertEqual(completed.status, RUN_STATUS_SUCCEEDED)

    def test_enqueue_creates_new_workflow_after_terminal_run(self) -> None:
        with self.session_factory() as session:
            first = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-906",
                precheck_outcome="ready_for_agent",
            )
            mark_run_terminal(
                session,
                run_id=first.run.run_id,
                terminal_status=RUN_STATUS_SUCCEEDED,
            )

            second = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-906",
                precheck_outcome="ready_for_agent",
            )
            self.assertTrue(second.enqueued)
            self.assertNotEqual(second.run.run_id, first.run.run_id)
            self.assertNotEqual(second.run.workflow_id, first.run.workflow_id)

    def test_enqueue_applies_bootstrap_before_queue_notification(self) -> None:
        observed: dict[str, object] = {}

        def capture_notification(session, *, tenant_id: str, project_id: str | None, run_id: str):  # noqa: ANN001
            observed["tenant_id"] = tenant_id
            observed["project_id"] = project_id
            observed["run_id"] = run_id
            staged_run = next(item for item in session.new if isinstance(item, Run))
            observed["plan"] = dict(staged_run.plan or {})
            observed["branch"] = staged_run.branch
            observed["pr_url"] = staged_run.pr_url
            observed["entry_mode"] = staged_run.entry_mode
            observed["entry_stage"] = staged_run.entry_stage
            observed["entry_checkpoint_id"] = staged_run.entry_checkpoint_id

        with self.session_factory() as session:
            with patch("orchestrator.core.runs.service.notify_run_enqueued", side_effect=capture_notification):
                result = enqueue_run(
                    session,
                    tenant_id="tenant-runs",
                    project_id="tenant-runs-default",
                    issue_key="TP-912",
                    precheck_outcome="ready_for_agent",
                    bootstrap=RunBootstrap(
                        branch="feature/TP-912",
                        pr_url="https://github.com/example/repo/pull/12",
                        entry_mode="resume",
                        entry_stage="dev",
                        entry_checkpoint_id="checkpoint-dev",
                        plan=ExecutionSnapshot.empty(
                            trigger_context={
                                "source": "manual",
                            }
                        ).dump(),
                    ),
                )

        self.assertTrue(result.enqueued)
        self.assertEqual(observed["tenant_id"], "tenant-runs")
        self.assertEqual(observed["project_id"], "tenant-runs-default")
        self.assertEqual(observed["run_id"], result.run.run_id)
        self.assertEqual(observed["branch"], "feature/TP-912")
        self.assertEqual(observed["pr_url"], "https://github.com/example/repo/pull/12")
        self.assertEqual(observed["entry_mode"], "resume")
        self.assertEqual(observed["entry_stage"], "dev")
        self.assertEqual(observed["entry_checkpoint_id"], "checkpoint-dev")
        self.assertEqual(
            observed["plan"],
            {
                "version": 1,
                "context": {
                    "trigger_context": {
                        "source": "manual",
                    },
                    "execution_context": {
                        "pre_check_outcome": "ready_for_agent",
                    },
                },
                "workflow": {
                    "outcome": None,
                    "attempts": 0,
                    "summary": [],
                    "blocker_message": None,
                    "requeue_target": None,
                    "requeue_reason": None,
                },
                "events": {
                    "stage_updates": [],
                    "live_stage_updates": [],
                    "stage_trace": [],
                    "workstream_trace": [],
                },
                "stages": {},
            },
        )

    def test_enqueue_attempt_rejects_non_ready_persisted_precheck(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            workflow = WorkflowExecution(
                workflow_id="workflow-non-ready",
                workflow_type_key="issue_execution",
                tenant_id="tenant-runs",
                project_id=None,
                source_system="jira",
                source_ref="TP-913",
                display_name="resume test",
                source_description="desc",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="temporal",
                dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                status="waiting_for_input",
                last_error=None,
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            session.add(workflow)
            session.commit()

            with self.assertRaisesRegex(
                RunStateTransitionError,
                "pre_check_outcome must be 'ready_for_agent'",
            ):
                enqueue_attempt_for_workflow_uncommitted(
                    session,
                    workflow_id="workflow-non-ready",
                    bootstrap=RunBootstrap(
                        workflow_id="workflow-non-ready",
                        entry_mode="resume",
                        entry_stage="pm",
                        plan=ExecutionSnapshot.empty(
                            trigger_context={"source": "manual"},
                        ).dump(),
                    ),
                )

    def test_resume_attempt_ignores_stale_blocked_active_run_pointer(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-914",
                precheck_outcome="ready_for_agent",
            )
            mark_run_terminal(
                session,
                run_id=enqueue.run.run_id,
                terminal_status=RUN_STATUS_BLOCKED,
                last_error="human_input_expired",
            )
            workflow = self._get_workflow(session, issue_key="TP-914")
            assert workflow is not None
            workflow.status = "running"
            workflow.active_run_id = enqueue.run.run_id
            session.commit()

            result = enqueue_attempt_for_workflow_uncommitted(
                session,
                workflow_id=workflow.workflow_id,
                bootstrap=RunBootstrap(
                    workflow_id=workflow.workflow_id,
                    parent_run_id=enqueue.run.run_id,
                    entry_mode="resume",
                    entry_stage="pm",
                    precheck_outcome="ready_for_agent",
                    plan=ExecutionSnapshot.empty().dump(),
                ),
            )

        self.assertTrue(result.enqueued)
        self.assertEqual(result.run.parent_run_id, enqueue.run.run_id)
        self.assertEqual(result.run.entry_mode, "resume")

    def test_persist_stage_checkpoint_writes_qa_execution_checkpoint_and_projects_pr_url(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-915",
                precheck_outcome="ready_for_agent",
            )
            running = mark_run_running(session, run_id=enqueue.run.run_id)
            running.worker_service_instance_id = self._WORKER_ID
            running.claim_id = self._CLAIM_ID
            running.pr_url = "https://github.com/example/repo/pull/15"
            session.commit()

            qa_result = QaResult(
                summary=["qa recorded 4 demos"],
                scenarios=[
                    QaScenario(
                        name="Fresh install",
                        objective="Prove the onboarding flow records successfully",
                        capture_target="ios",
                    )
                ],
                recordings=[
                    QaRecording(
                        name="Fresh install",
                        artifact_url="https://cdn.example/qa-demo-1.mp4",
                        object_key="tenant-runs/default/TP-915/qa-demo-1.mp4",
                        capture_reference="ios-simulator://configured",
                        capture_target="ios",
                    )
                ],
            )

            persist_stage_checkpoint(
                session,
                run=running,
                checkpoint=WorkflowStageCheckpoint(
                    stage="qa",
                    attempt=1,
                    status="completed",
                    summary="qa recorded 4 demos",
                    qa_result=qa_result,
                ),
                expected_worker_service_instance_id=self._WORKER_ID,
                expected_claim_id=self._CLAIM_ID,
            )

            workflow = self._get_workflow(session, issue_key="TP-915")
            assert workflow is not None
            checkpoint = session.query(WorkflowCheckpoint).filter_by(
                run_id=running.run_id,
                checkpoint_kind="execution",
            ).one()

            self.assertEqual(checkpoint.stage, "qa")
            self.assertEqual(workflow.latest_checkpoint_id, checkpoint.checkpoint_id)
            self.assertEqual(workflow.pr_url, "https://github.com/example/repo/pull/15")

    def test_finalize_workflow_result_preserves_existing_pr_url_when_result_omits_it(self) -> None:
        with self.session_factory() as session:
            enqueue = enqueue_run(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-916",
                precheck_outcome="ready_for_agent",
            )
            running = mark_run_running(session, run_id=enqueue.run.run_id)
            running.worker_service_instance_id = self._WORKER_ID
            running.claim_id = self._CLAIM_ID
            running.pr_url = "https://github.com/example/repo/pull/16"
            session.commit()

            finalized = finalize_workflow_result(
                session,
                run=running,
                workflow_result=WorkflowResult(
                    outcome="success",
                    plan=None,
                    pr_url=None,
                    summary=["completed"],
                    test_guidance=[],
                    attempts=1,
                ),
                stage_updates=[],
                expected_worker_service_instance_id=self._WORKER_ID,
                expected_claim_id=self._CLAIM_ID,
            )

            workflow = self._get_workflow(session, issue_key="TP-916")
            assert workflow is not None

            self.assertEqual(finalized.status, RUN_STATUS_SUCCEEDED)
            self.assertEqual(finalized.pr_url, "https://github.com/example/repo/pull/16")
            self.assertEqual(workflow.pr_url, "https://github.com/example/repo/pull/16")
