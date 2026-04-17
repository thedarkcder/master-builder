import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from orchestrator.core.config import get_settings
from orchestrator.core.runs import (
    RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
    RUN_DEDUPE_SCOPE_PR_REMEDIATION,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_SUCCEEDED,
    RunBootstrap,
    RunStateTransitionError,
    enqueue_attempt_for_workflow_uncommitted,
    enqueue_run,
    mark_run_running,
    mark_run_terminal,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Run, Tenant, WorkflowExecution


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
            issue_key=issue_key,
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

    def test_failure_path_marks_blocked_without_finishing_workflow(self) -> None:
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
            self.assertEqual(workflow.status, RUN_STATUS_BLOCKED)
            self.assertEqual(workflow.blocked_reason, "jira label mutation failed")
            self.assertIsNone(workflow.finished_at)

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

        def capture_notification(session, *, tenant_id: str, project_id: str | None, run_id: str, issue_key: str):  # noqa: ANN001
            observed["tenant_id"] = tenant_id
            observed["project_id"] = project_id
            observed["run_id"] = run_id
            observed["issue_key"] = issue_key
            staged_run = next(item for item in session.new if isinstance(item, Run))
            observed["plan"] = dict(staged_run.plan or {})
            observed["branch"] = staged_run.branch
            observed["pr_url"] = staged_run.pr_url
            observed["entry_mode"] = staged_run.entry_mode
            observed["entry_stage"] = staged_run.entry_stage
            observed["entry_checkpoint_id"] = staged_run.entry_checkpoint_id

        with self.session_factory() as session:
            with patch("orchestrator.core.runs.notify_run_enqueued", side_effect=capture_notification):
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
        self.assertEqual(observed["issue_key"], "TP-912")
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
                issue_key="TP-913",
                issue_summary="resume test",
                issue_description="desc",
                repo_url=None,
                branch=None,
                pr_url=None,
                dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                status="waiting_for_input",
                last_error=None,
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                blocked_reason=None,
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
