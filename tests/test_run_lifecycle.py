import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError

from orchestrator.core.config import get_settings
from orchestrator.core.runs import (
    RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
    RUN_DEDUPE_SCOPE_PR_REMEDIATION,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_SUCCEEDED,
    RunBootstrap,
    RunStateTransitionError,
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

    def _enqueue_run_without_temporal_start(self, session, **kwargs):  # noqa: ANN001
        with patch("orchestrator.core.runs.start_team_run_workflow_for_run"):
            return enqueue_run(session, **kwargs)

    def test_enqueue_is_idempotent_for_active_issue(self) -> None:
        with self.session_factory() as session:
            first = self._enqueue_run_without_temporal_start(session, tenant_id="tenant-runs", project_id=None, issue_key="TP-901")
            second = self._enqueue_run_without_temporal_start(session, tenant_id="tenant-runs", project_id=None, issue_key="TP-901")

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
            issue_run = self._enqueue_run_without_temporal_start(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-907",
                dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
            )
            remediation_run = self._enqueue_run_without_temporal_start(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-907",
                dedupe_scope=RUN_DEDUPE_SCOPE_PR_REMEDIATION,
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
            first = self._enqueue_run_without_temporal_start(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-902",
                delivery_id="delivery-xyz",
            )
            second = self._enqueue_run_without_temporal_start(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-902",
                delivery_id="delivery-xyz",
            )

            self.assertTrue(first.enqueued)
            self.assertFalse(second.enqueued)
            self.assertEqual(second.reason, "duplicate_delivery")
            self.assertEqual(second.run.run_id, first.run.run_id)

    def test_enqueue_respects_tenant_concurrency_limit(self) -> None:
        with self.session_factory() as session:
            first = self._enqueue_run_without_temporal_start(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-910",
                max_concurrent_runs=1,
            )
            second = self._enqueue_run_without_temporal_start(
                session,
                tenant_id="tenant-runs",
                project_id=None,
                issue_key="TP-911",
                max_concurrent_runs=1,
            )

            self.assertTrue(first.enqueued)
            self.assertFalse(second.enqueued)
            self.assertEqual(second.reason, "tenant_concurrency_limit_reached")
            self.assertEqual(second.run.run_id, first.run.run_id)

    def test_running_to_success_updates_workflow_and_persists_timestamps(self) -> None:
        with self.session_factory() as session:
            enqueue = self._enqueue_run_without_temporal_start(session, tenant_id="tenant-runs", project_id=None, issue_key="TP-903")
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
            enqueue = self._enqueue_run_without_temporal_start(session, tenant_id="tenant-runs", project_id=None, issue_key="TP-904")
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
            enqueue = self._enqueue_run_without_temporal_start(session, tenant_id="tenant-runs", project_id=None, issue_key="TP-905")
            mark_run_terminal(
                session,
                run_id=enqueue.run.run_id,
                terminal_status=RUN_STATUS_SUCCEEDED,
            )

            with self.assertRaises(RunStateTransitionError):
                mark_run_running(session, run_id=enqueue.run.run_id)

    def test_enqueue_creates_new_workflow_after_terminal_run(self) -> None:
        with self.session_factory() as session:
            first = self._enqueue_run_without_temporal_start(session, tenant_id="tenant-runs", project_id=None, issue_key="TP-906")
            mark_run_terminal(
                session,
                run_id=first.run.run_id,
                terminal_status=RUN_STATUS_SUCCEEDED,
            )

            second = self._enqueue_run_without_temporal_start(session, tenant_id="tenant-runs", project_id=None, issue_key="TP-906")
            self.assertTrue(second.enqueued)
            self.assertNotEqual(second.run.run_id, first.run.run_id)
            self.assertNotEqual(second.run.workflow_id, first.run.workflow_id)

    def test_enqueue_run_starts_temporal_team_workflow_for_issue_runs(self) -> None:
        previous_backend = os.environ.get("ORCHESTRATOR_ORCHESTRATION_BACKEND")
        try:
            os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = "temporal"
            get_settings.cache_clear()
            with self.session_factory() as session:
                with patch("orchestrator.core.runs.start_team_run_workflow_for_run") as start_mock:
                    result = enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-912",
                    )

                self.assertTrue(result.enqueued)
                start_mock.assert_called_once()
                start_mock.assert_called_once_with(settings=get_settings(), run=result.run)
                snapshot = ExecutionSnapshot.load(result.run.plan)
                assert snapshot is not None
                self.assertEqual(
                    snapshot.context.execution_context.get("orchestration_backend"),
                    "temporal",
                )
                self.assertIsInstance(snapshot.context.execution_context.get("team_run"), dict)
        finally:
            if previous_backend is None:
                os.environ.pop("ORCHESTRATOR_ORCHESTRATION_BACKEND", None)
            else:
                os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = previous_backend
            get_settings.cache_clear()

    def test_enqueue_team_run_starts_team_temporal_workflow_without_legacy_fallback(self) -> None:
        previous_backend = os.environ.get("ORCHESTRATOR_ORCHESTRATION_BACKEND")
        try:
            os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = "legacy"
            get_settings.cache_clear()
            snapshot = ExecutionSnapshot.empty()
            snapshot.context.execution_context["team_run"] = {
                "team_key": "campaign_team",
                "team_label": "Campaign Team",
                "definition_version": 1,
                "nodes": [
                    {
                        "task_key": "brief",
                        "label": "Brief",
                        "owner_role_key": "strategist",
                        "owner_persona_key": "launch_strategist",
                        "owner_agent_key": "launch_strategy_agent",
                        "status": "ready",
                        "dependency_keys": [],
                        "artifact_contract": {},
                        "approval_rule": {},
                    }
                ],
                "edges": [],
                "artifacts": [],
                "approvals": [],
                "status": "queued",
            }
            with self.session_factory() as session:
                with patch("orchestrator.core.runs.start_team_run_workflow_for_run") as start_team_mock:
                    result = enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-916",
                        bootstrap=RunBootstrap(
                            entry_mode="fresh",
                            entry_stage="team",
                            plan=snapshot.dump(),
                        ),
                    )

                self.assertTrue(result.enqueued)
                start_team_mock.assert_called_once_with(settings=get_settings(), run=result.run)
                persisted = session.get(Run, result.run.run_id)
                assert persisted is not None
                run_snapshot = ExecutionSnapshot.load(persisted.plan)
                assert run_snapshot is not None
                self.assertEqual(run_snapshot.context.execution_context.get("orchestration_backend"), "temporal")
        finally:
            if previous_backend is None:
                os.environ.pop("ORCHESTRATOR_ORCHESTRATION_BACKEND", None)
            else:
                os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = previous_backend
            get_settings.cache_clear()

    def test_enqueue_run_marks_failed_when_temporal_start_fails(self) -> None:
        previous_backend = os.environ.get("ORCHESTRATOR_ORCHESTRATION_BACKEND")
        try:
            os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = "temporal"
            get_settings.cache_clear()
            with self.session_factory() as session:
                with patch(
                    "orchestrator.core.runs.start_team_run_workflow_for_run",
                    side_effect=RuntimeError("temporal unavailable"),
                ):
                    result = enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-913",
                    )

                self.assertTrue(result.enqueued)
                session.refresh(result.run)
                snapshot = ExecutionSnapshot.load(result.run.plan)
                assert snapshot is not None
                self.assertEqual(
                    snapshot.context.execution_context.get("orchestration_backend"),
                    "temporal",
                )
                self.assertEqual(result.run.status, "failed")
                self.assertIn("Temporal team workflow start failed", str(result.run.last_error or ""))
        finally:
            if previous_backend is None:
                os.environ.pop("ORCHESTRATOR_ORCHESTRATION_BACKEND", None)
            else:
                os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = previous_backend
            get_settings.cache_clear()

    def test_enqueue_attempt_for_temporal_workflow_starts_temporal_for_each_attempt(self) -> None:
        previous_backend = os.environ.get("ORCHESTRATOR_ORCHESTRATION_BACKEND")
        try:
            os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = "temporal"
            get_settings.cache_clear()
            with self.session_factory() as session:
                with patch("orchestrator.core.runs.start_team_run_workflow_for_run") as start_mock:
                    first = enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-914",
                    )
                    workflow = session.get(WorkflowExecution, first.run.workflow_id)
                    assert workflow is not None
                    workflow.status = "waiting_for_input"
                    session.commit()
                    attempted = enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-914",
                        bootstrap=RunBootstrap(
                            workflow_id=first.run.workflow_id,
                            parent_run_id=first.run.run_id,
                            entry_mode="resume",
                            entry_stage="review",
                            plan=ExecutionSnapshot.empty().dump(),
                        ),
                    )

                self.assertTrue(first.enqueued)
                self.assertTrue(attempted.enqueued)
                self.assertEqual(start_mock.call_count, 2)
                self.assertEqual(attempted.run.workflow_id, first.run.workflow_id)
        finally:
            if previous_backend is None:
                os.environ.pop("ORCHESTRATOR_ORCHESTRATION_BACKEND", None)
            else:
                os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = previous_backend
            get_settings.cache_clear()

    def test_enqueue_attempt_integrity_conflict_returns_transition_error(self) -> None:
        with self.session_factory() as session:
            with patch("orchestrator.core.runs.start_team_run_workflow_for_run"):
                first = enqueue_run(
                    session,
                    tenant_id="tenant-runs",
                    project_id=None,
                    issue_key="TP-918",
                )
            workflow = session.get(WorkflowExecution, first.run.workflow_id)
            assert workflow is not None
            workflow.status = "waiting_for_input"
            session.commit()

            with patch.object(
                session,
                "commit",
                side_effect=IntegrityError("insert into runs", {"attempt_number": 2}, Exception("duplicate")),
            ):
                with self.assertRaises(RunStateTransitionError) as exc_info:
                    enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-918",
                        bootstrap=RunBootstrap(
                            workflow_id=workflow.workflow_id,
                            parent_run_id=first.run.run_id,
                            entry_mode="resume",
                            entry_stage="review",
                            plan=ExecutionSnapshot.empty().dump(),
                        ),
                    )

            self.assertIn("concurrent attempt creation", str(exc_info.exception))

    def test_resume_attempt_for_temporal_workflow_keeps_temporal_ownership(self) -> None:
        previous_backend = os.environ.get("ORCHESTRATOR_ORCHESTRATION_BACKEND")
        try:
            os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = "temporal"
            get_settings.cache_clear()
            with self.session_factory() as session:
                with patch("orchestrator.core.runs.start_team_run_workflow_for_run"):
                    first = enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-915",
                    )

                workflow = session.get(WorkflowExecution, first.run.workflow_id)
                assert workflow is not None
                workflow.status = "waiting_for_input"
                session.commit()

                with patch("orchestrator.core.runs.start_team_run_workflow_for_run"):
                    resumed = enqueue_run(
                        session,
                        tenant_id="tenant-runs",
                        project_id=None,
                        issue_key="TP-915",
                        bootstrap=RunBootstrap(
                            workflow_id=first.run.workflow_id,
                            parent_run_id=first.run.run_id,
                            entry_mode="resume",
                            entry_stage="review",
                            plan=ExecutionSnapshot.empty().dump(),
                        ),
                    )

                snapshot = ExecutionSnapshot.load(resumed.run.plan)
                assert snapshot is not None
                self.assertEqual(
                    snapshot.context.execution_context.get("orchestration_backend"),
                    "temporal",
                )
                self.assertIsInstance(snapshot.context.execution_context.get("team_run"), dict)
        finally:
            if previous_backend is None:
                os.environ.pop("ORCHESTRATOR_ORCHESTRATION_BACKEND", None)
            else:
                os.environ["ORCHESTRATOR_ORCHESTRATION_BACKEND"] = previous_backend
            get_settings.cache_clear()

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
            with (
                patch("orchestrator.core.runs.notify_run_enqueued", side_effect=capture_notification),
                patch("orchestrator.core.runs.start_team_run_workflow_for_run"),
            ):
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
        observed_plan = ExecutionSnapshot.require(observed["plan"])
        self.assertEqual(
            observed_plan.context.trigger_context,
            {
                "source": "manual",
            },
        )
        self.assertEqual(
            observed_plan.context.execution_context.get("pre_check_outcome"),
            "ready_for_agent",
        )
        self.assertEqual(
            observed_plan.context.execution_context.get("orchestration_backend"),
            "temporal",
        )
        self.assertIsInstance(observed_plan.context.execution_context.get("team_run"), dict)
