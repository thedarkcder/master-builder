import json
import os
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.config import get_settings
from orchestrator.core.agent_observability import (
    agent_observability_tracker,
    reset_agent_observability_for_tests,
)
from orchestrator.core.runs import enqueue_run
from orchestrator.core.worker_capability_normalization import WorkerCapability
from orchestrator.core.workflow.runner import (
    PmPlan,
    WorkflowStageCheckpoint,
    WorkflowDiagnostics,
    WorkflowResult,
)
from orchestrator.core.discord.notifications import DiscordSendResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant, WorkflowExecution
from orchestrator.worker import process_next_queued_run
from orchestrator.tools.project_repo_checkout import ProjectRepoCheckoutError
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class _SuccessRunner:
    def __init__(self) -> None:
        self.last_request = None

    def run(self, request, *, test_feedback_hook=None, stage_checkpoint_hook=None):  # noqa: ANN001,ARG002
        self.last_request = request
        if stage_checkpoint_hook is not None:
            stage_checkpoint_hook(
                WorkflowStageCheckpoint(
                    stage="pm",
                    attempt=1,
                    status="completed",
                    summary="PM completed",
                    plan=PmPlan(
                        plan_steps=["plan", "build", "validate"],
                        acceptance_criteria=["has PR link"],
                        risks=[],
                    ),
                )
            )
        return WorkflowResult(
            outcome="success",
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
    def run(self, request, *, test_feedback_hook=None, stage_checkpoint_hook=None):  # noqa: ANN001,ARG002
        _ = stage_checkpoint_hook
        return WorkflowResult(
            outcome="failed",
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


class _CapabilityMismatchRunner:
    def run(self, request, *, test_feedback_hook=None, stage_checkpoint_hook=None):  # noqa: ANN001,ARG002
        _ = stage_checkpoint_hook
        return WorkflowResult(
            outcome="requeue",
            plan=PmPlan(
                plan_steps=["Plan implementation"],
                acceptance_criteria=["Feature implemented"],
                risks=["Requires macOS worker"],
                execution_worker_capability="macos",
            ),
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            requeue_target=WorkerCapability.MACOS,
            requeue_reason=(
                "Execution capability mismatch: PM selected macos but current worker is linux. "
                "Requeue on worker:macos before dev/test/review."
            ),
            diagnostics=WorkflowDiagnostics(
                stage="pm",
                message=(
                    "Execution capability mismatch: PM selected macos but current worker is linux. "
                    "Requeue on worker:macos before dev/test/review."
                ),
                attempts=1,
                history=[
                    {
                        "stage": "pm",
                        "attempt": "1",
                        "event": "execution_capability_mismatch:required=macos,current=linux",
                    }
                ],
            ),
        )


class WorkerWorkflowTests(SqliteTemplateDbTestCase):
    @classmethod
    def bootstrap_template_database(cls) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(database_url=cls._template_database_url)
        with session_factory() as session:
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
                        "github_repository": "https://github.com/example/repo",
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
            session.add(
                Project(
                    project_id="tenant-worker-default",
                    tenant_id="tenant-worker",
                    name="Default Project",
                    github_repository="https://github.com/example/repo",
                    jira_project_key="TP",
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self._original_database_url = os.environ.get("ORCHESTRATOR_DATABASE_URL")
        self.database_url = self._prepare_test_database(name_prefix="worker-workflow")
        self.repo_checkout_base_dir = f"{self.temp_dir.name}/project-repos"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = self.repo_checkout_base_dir
        os.environ["ORCHESTRATOR_WORKER_WORKSPACE_KEY"] = "worker-a"
        os.environ["ORCHESTRATOR_WORKER_CAPABILITIES"] = "linux"
        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_agent_observability_for_tests()
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._jira_issue_details: dict[str, dict[str, object]] = {}
        self.checkout_patcher = patch(
            "orchestrator.core.worker.execution_service.ensure_project_repository_checkout"
        )
        self.checkout_mock = self.checkout_patcher.start()
        self.worktree_patcher = patch(
            "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
            side_effect=self._ensure_run_worktree_stub,
        )
        self.worktree_patcher.start()
        self.worktree_validate_patcher = patch(
            "orchestrator.core.worker.workflow_request_service.validate_run_worktree",
            return_value=None,
        )
        self.worktree_validate_patcher.start()
        self.freshness_patcher = patch(
            "orchestrator.core.worker.execution_service.check_run_snapshot_freshness",
            return_value=SimpleNamespace(stale=False, message=None),
        )
        self.freshness_patcher.start()
        self.jira_oauth_patcher = patch(
            "orchestrator.core.worker.execution_service.tenant_jira_oauth_context",
            side_effect=self._tenant_jira_oauth_context_stub,
        )
        self.jira_oauth_patcher.start()
        self._seed_checked_out_repo()

    def tearDown(self) -> None:
        self.checkout_patcher.stop()
        self.worktree_patcher.stop()
        self.worktree_validate_patcher.stop()
        self.freshness_patcher.stop()
        self.temp_dir.cleanup()
        self._cleanup_test_database()
        if self._original_database_url is None:
            os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        else:
            os.environ["ORCHESTRATOR_DATABASE_URL"] = self._original_database_url
        os.environ.pop("ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR", None)
        os.environ.pop("ORCHESTRATOR_WORKER_WORKSPACE_KEY", None)
        os.environ.pop("ORCHESTRATOR_WORKER_CAPABILITIES", None)
        self.jira_oauth_patcher.stop()
        get_settings.cache_clear()
        reset_db_engine_cache()
        reset_agent_observability_for_tests()

    def _tenant_jira_oauth_context_stub(self, *, session, tenant, settings):  # noqa: ANN001
        _ = session, tenant, settings

        class _Client:
            def __init__(self, issue_details: dict[str, dict[str, object]]) -> None:
                self.issue_details = issue_details

            def get_issue_detail(self, *, access_token: str, cloud_id: str, issue_id_or_key: str):  # noqa: ARG002
                detail = self.issue_details.get(issue_id_or_key, {})
                labels = detail["labels"] if "labels" in detail else ["agent:ready"]
                return SimpleNamespace(
                    key=issue_id_or_key,
                    summary=str(detail.get("summary") or f"Implement {issue_id_or_key}"),
                    description=str(detail.get("description") or ""),
                    labels=list(labels),
                )

        return SimpleNamespace(
            client=_Client(self._jira_issue_details),
            connection=SimpleNamespace(cloud_id="cloud-1"),
            access_token="access-token",
        )

    def _queue_run(
        self,
        issue_key: str,
        *,
        issue_summary: str | None = None,
        issue_description: str | None = None,
    ) -> str:
        effective_summary = issue_summary or f"Implement {issue_key}"
        effective_description = issue_description or (
            "Objective: Deliver requested behavior. "
            "Scope: in scope and out of scope are documented. "
            "Acceptance Criteria: all required checks pass. "
            "How to test: run unit tests and validate expected outputs. "
            "NFR intent: MVP."
        )
        with self.session_factory() as session:
            result = enqueue_run(
                session,
                tenant_id="tenant-worker",
                project_id="tenant-worker-default",
                issue_key=issue_key,
                issue_summary=effective_summary,
                issue_description=effective_description,
                repo_url="https://github.com/example/repo",
            )
            self.assertTrue(result.enqueued)
        self._jira_issue_details[issue_key] = {
            "summary": effective_summary,
            "description": effective_description,
            "labels": ["agent:ready"],
        }
        return result.run.run_id

    def _seed_checked_out_repo(self) -> None:
        repo_git_dir = (
            f"{self.repo_checkout_base_dir}/tenant-worker/tenant-worker-default/repo/.git"
        )
        os.makedirs(repo_git_dir, exist_ok=True)

    def _run_workspaces_dir(self, run_id: str) -> Path:
        return (
            Path(self.repo_checkout_base_dir)
            / "tenant-worker"
            / "tenant-worker-default"
            / "runs"
            / run_id
            / "workspaces"
        )

    def _ensure_run_worktree_stub(
        self,
        *,
        base_dir: str,
        tenant_id: str,
        project,
        run_id: str,
        issue_key: str,
        base_branch: str,
        integration_branch: str,
        workspace_key: str,
    ) -> tuple[str, str]:
        _ = (issue_key, base_branch, integration_branch)
        worktree_dir = (
            Path(base_dir)
            / tenant_id
            / project.project_id
            / "runs"
            / run_id
            / "workspaces"
            / workspace_key
            / "repo"
        )
        (worktree_dir / ".git").mkdir(parents=True, exist_ok=True)
        (worktree_dir / ".master-builder-run.json").write_text(
            json.dumps(
                {
                    "run_id": run_id,
                    "issue_key": issue_key,
                    "execution_branch": f"run/{issue_key}/{run_id}",
                    "base_branch": base_branch,
                    "integration_branch": integration_branch,
                    "workspace_key": workspace_key,
                    "start_point_ref": "origin/main",
                    "start_point_sha": "startsha123",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return str(worktree_dir), f"run/{issue_key}/{run_id}"

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
            self.assertEqual(processed.plan["workflow"]["outcome"], "success")
            self.assertEqual(processed.plan["workflow"]["attempts"], 1)
            self.assertEqual(processed.plan["stages"]["pm"]["artifact"]["plan_steps"], ["plan", "build", "validate"])
            self.assertEqual(processed.plan["stages"]["pm"]["status"], "completed")
            stage_updates = processed.plan["events"]["stage_updates"]
            self.assertEqual(
                [entry["stage"] for entry in stage_updates],
                ["lock_acquired", "plan_posted", "pr_opened"],
            )
            self.assertIn("TP-300", stage_updates[0]["discord_message"])
            self.assertIn(processed.run_id, stage_updates[0]["discord_message"])
            self.assertIsNotNone(runner.last_request)
            self.assertTrue(
                str(runner.last_request.execution_repo_dir).endswith(
                    f"/tenant-worker/tenant-worker-default/runs/{processed.run_id}/workspaces/worker-a/repo"
                )
            )
            self.assertFalse(self._run_workspaces_dir(run_id).exists())

        events, _ = agent_observability_tracker.snapshot()
        event_types = [event.event_type for event in events]
        self.assertIn("ISSUE_ASSIGNED", event_types)
        self.assertIn("TASK_STARTED", event_types)
        self.assertIn("TASK_COMPLETED", event_types)

    def test_process_next_queued_run_requeues_when_branch_snapshot_is_stale(self) -> None:
        run_id = self._queue_run("TP-3001")
        runner = _SuccessRunner()

        with (
            patch(
                "orchestrator.core.worker.execution_service.check_run_snapshot_freshness",
                return_value=SimpleNamespace(
                    stale=True,
                    message="Branch snapshot stale: origin/main moved from startsha123 to newsha456.",
                ),
            ),
            self.session_factory() as session,
        ):
            processed = process_next_queued_run(session, runner)
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "queued")
            self.assertIsNone(processed.last_error)
            self.assertIsNone(processed.started_at)
            self.assertIsNone(processed.finished_at)
            self.assertIsNone(processed.pr_url)
            self.assertTrue(processed.plan["context"]["execution_context"]["stale_branch_snapshot"])
            self.assertIn("Branch snapshot stale", processed.plan["workflow"]["requeue_reason"])
            stage_updates = processed.plan["events"]["stage_updates"]
            self.assertEqual(
                [entry["stage"] for entry in stage_updates],
                ["lock_acquired", "plan_posted", "run_requeued_stale_snapshot"],
            )
            self.assertFalse(self._run_workspaces_dir(run_id).exists())
            workflow = session.get(WorkflowExecution, processed.workflow_id)
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "queued")
            self.assertEqual(workflow.active_run_id, run_id)

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
            self.assertEqual(processed.plan["workflow"]["outcome"], "failed")
            stage_updates = processed.plan["events"]["stage_updates"]
            self.assertEqual(
                [entry["stage"] for entry in stage_updates],
                ["lock_acquired", "plan_posted", "run_failed"],
            )
            self.assertIn(
                "Max workflow attempts reached after test failures",
                stage_updates[-1]["jira_message"],
            )

        events, _ = agent_observability_tracker.snapshot()
        event_types = [event.event_type for event in events]
        self.assertIn("TASK_FAILED", event_types)
        self.assertIn("TEST_FAILED", event_types)

    def test_process_next_queued_run_requeues_when_pm_requires_different_worker_capability(self) -> None:
        run_id = self._queue_run("TP-3020")

        with self.session_factory() as session:
            processed = process_next_queued_run(session, _CapabilityMismatchRunner())
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "queued")
            self.assertIsNone(processed.last_error)
            self.assertIsNone(processed.started_at)
            self.assertIsNone(processed.finished_at)
            self.assertIsInstance(processed.plan, dict)
            self.assertEqual(processed.plan["workflow"]["requeue_target"], "macos")
            self.assertEqual(processed.plan["context"]["execution_context"]["required_worker_label"], "worker:macos")
            stage_updates = processed.plan["events"]["stage_updates"]
            self.assertEqual(
                [entry["stage"] for entry in stage_updates],
                ["lock_acquired", "plan_posted", "run_requeued_capability_mismatch"],
            )
            self.assertFalse(self._run_workspaces_dir(run_id).exists())
            workflow = session.get(WorkflowExecution, processed.workflow_id)
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "queued")
            self.assertEqual(workflow.active_run_id, run_id)

        events, _ = agent_observability_tracker.snapshot()
        event_types = [event.event_type for event in events]
        self.assertIn("TASK_STARTED", event_types)
        self.assertNotIn("TASK_FAILED", event_types)

    def test_process_next_queued_run_blocks_when_ready_label_is_missing(self) -> None:
        run_id = self._queue_run(
            "TP-302",
            issue_summary="Unclear requirements",
            issue_description="TBD: need to decide later?",
        )
        self._jira_issue_details["TP-302"]["labels"] = []

        with self.session_factory() as session:
            processed = process_next_queued_run(session, _SuccessRunner())
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "blocked")
            self.assertIn("Issue is missing the configured ready label", processed.last_error or "")
            self.assertIsInstance(processed.plan, dict)
            self.assertIn("run_not_ready", processed.plan["context"]["execution_context"])
            stage_updates = processed.plan["events"]["stage_updates"]
            self.assertEqual([entry["stage"] for entry in stage_updates], ["run_not_ready"])
            retry_enqueue = enqueue_run(
                session,
                tenant_id="tenant-worker",
                project_id=None,
                issue_key="TP-302",
                issue_summary="Clarified requirements",
                issue_description=(
                    "Objective: deliver requested behavior. "
                    "Scope: explicit in/out scope. "
                    "Acceptance Criteria: measurable checks. "
                    "How to test: exact commands and expected outcomes. "
                    "NFR intent: MVP."
                ),
                repo_url="https://github.com/example/repo",
            )
            self.assertFalse(retry_enqueue.enqueued)
            self.assertEqual(retry_enqueue.reason, "run_already_active")
            self.assertEqual(retry_enqueue.run.workflow_id, processed.workflow_id)

    def test_process_next_queued_run_fails_and_releases_lock_on_decision_gate_exception(self) -> None:
        run_id = self._queue_run("TP-3021")

        with self.session_factory() as session, patch(
            "orchestrator.core.worker.execution_service.apply_decision_gate",
            side_effect=RuntimeError("decision gate parse failed"),
        ):
            processed = process_next_queued_run(session, _SuccessRunner())
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "failed")
            self.assertIn("Decision Gate evaluation failed: decision gate parse failed", processed.last_error or "")
            workflow = session.get(WorkflowExecution, processed.workflow_id)
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "failed")

    def test_run_not_ready_notification_does_not_open_reply_thread(self) -> None:
        run_id = self._queue_run(
            "TP-399",
            issue_summary="Unclear requirements",
            issue_description="TBD: need to decide later?",
        )
        self._jira_issue_details["TP-399"]["labels"] = []

        with self.session_factory() as session, patch(
            "orchestrator.worker.send_tenant_discord_message",
            return_value=DiscordSendResult(sent=True, reason="sent"),
        ) as send_mock:
            processed = process_next_queued_run(session, _SuccessRunner())
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "blocked")

        send_mock.assert_called_once()
        kwargs = send_mock.call_args.kwargs
        self.assertFalse(kwargs["open_thread"])
        self.assertNotIn("thread_intro_components", kwargs)

    def test_process_next_queued_run_missing_project_mapping_releases_run_lock(self) -> None:
        with self.session_factory() as session:
            enqueue_result = enqueue_run(
                session,
                tenant_id="tenant-worker",
                project_id=None,
                issue_key="ZZ-101",
                issue_summary="Missing project mapping",
                issue_description=(
                    "Objective: validate missing project behavior. "
                    "Scope: worker should fail safely. "
                    "Acceptance Criteria: lock removed. "
                    "How to test: process queued run and verify lock is cleared. "
                    "NFR intent: MVP."
                ),
                repo_url="https://github.com/example/repo",
            )
            self.assertTrue(enqueue_result.enqueued)
            run_id = enqueue_result.run.run_id

        with self.session_factory() as session:
            processed = process_next_queued_run(session, _SuccessRunner())
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "failed")
            self.assertEqual(
                processed.last_error,
                "No active project mapping found for issue ZZ-101",
            )
            workflow = session.get(WorkflowExecution, processed.workflow_id)
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "failed")

    def test_process_next_queued_run_uses_tenant_jira_site_url_for_stage_links(self) -> None:
        run_id = self._queue_run("TP-555")
        now = datetime.now(timezone.utc)

        with self.session_factory() as session:
            session.add(
                JiraOAuthConnection(
                    connection_id="jira-tenant-worker",
                    account_id="acct-1",
                    account_email="agent@example.com",
                    cloud_id="cloud-1",
                    site_url="https://jira.example.test",
                    scopes=["read:jira-work"],
                    access_token_encrypted="enc-access",
                    refresh_token_encrypted="enc-refresh",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            tenant = session.get(Tenant, "tenant-worker")
            assert tenant is not None
            jira_config = dict(tenant.jira_config or {})
            jira_config["connection_id"] = "jira-tenant-worker"
            tenant.jira_config = jira_config
            tenant.updated_at = now
            session.commit()

        runner = _SuccessRunner()
        with self.session_factory() as session:
            processed = process_next_queued_run(session, runner)
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            stage_updates = processed.plan["events"]["stage_updates"]
            self.assertIn(
                "https://jira.example.test/browse/TP-555",
                stage_updates[0]["discord_message"],
            )

    def test_process_next_queued_run_ensures_project_repository_checkout(self) -> None:
        run_id = self._queue_run("TP-556")
        runner = _SuccessRunner()

        with self.session_factory() as session:
            processed = process_next_queued_run(session, runner)
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "succeeded")

        self.checkout_mock.assert_called()
        checkout_kwargs = self.checkout_mock.call_args.kwargs
        self.assertEqual(checkout_kwargs["tenant"].tenant_id, "tenant-worker")
        self.assertEqual(checkout_kwargs["project"].project_id, "tenant-worker-default")

    def test_process_next_queued_run_fails_when_project_checkout_fails(self) -> None:
        run_id = self._queue_run("TP-557")
        runner = _SuccessRunner()
        self.checkout_mock.side_effect = ProjectRepoCheckoutError("clone failed")

        with self.session_factory() as session:
            processed = process_next_queued_run(session, runner)
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "failed")
            self.assertEqual(
                processed.last_error,
                "Project repository checkout failed: clone failed",
            )

        self.assertIsNone(runner.last_request)
