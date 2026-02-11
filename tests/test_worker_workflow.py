import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from orchestrator.core.config import get_settings
from orchestrator.core.runs import enqueue_run
from orchestrator.core.workflow.runner import (
    PmPlan,
    WorkflowDiagnostics,
    WorkflowResult,
)
from orchestrator.core.discord.notifications import DiscordSendResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, RunLock, Tenant
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

    def _queue_run(
        self,
        issue_key: str,
        *,
        issue_summary: str | None = None,
        issue_description: str | None = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        run_id = f"run-{issue_key}"
        effective_summary = issue_summary or f"Implement {issue_key}"
        effective_description = issue_description or (
            "Objective: Deliver requested behavior. "
            "Scope: in scope and out of scope are documented. "
            "Acceptance Criteria: all required checks pass. "
            "How to test: run unit tests and validate expected outputs. "
            "NFR intent: MVP."
        )
        with self.session_factory() as session:
            session.add(
                Run(
                    run_id=run_id,
                    tenant_id="tenant-worker",
                    issue_key=issue_key,
                    issue_summary=effective_summary,
                    issue_description=effective_description,
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
            stage_updates = processed.plan["stage_updates"]
            self.assertEqual(
                [entry["stage"] for entry in stage_updates],
                ["lock_acquired", "plan_posted", "pr_opened"],
            )
            self.assertIn("TP-300", stage_updates[0]["discord_message"])
            self.assertIn("run-TP-300", stage_updates[0]["discord_message"])
            self.assertIsNotNone(runner.last_request)
            self.assertIn("Good To Do", runner.last_request.issue_description)
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
            stage_updates = processed.plan["stage_updates"]
            self.assertEqual(
                [entry["stage"] for entry in stage_updates],
                ["lock_acquired", "plan_posted", "run_failed"],
            )
            self.assertIn(
                "Max workflow attempts reached after test failures",
                stage_updates[-1]["jira_message"],
            )

    def test_process_next_queued_run_blocks_when_decision_gate_is_required(self) -> None:
        run_id = self._queue_run(
            "TP-302",
            issue_summary="Unclear requirements",
            issue_description="TBD: need to decide later?",
        )

        with self.session_factory() as session:
            processed = process_next_queued_run(session, _SuccessRunner())
            self.assertIsNotNone(processed)
            self.assertEqual(processed.run_id, run_id)
            self.assertEqual(processed.status, "blocked")
            self.assertIn("Decision Gate required", processed.last_error or "")
            self.assertIsInstance(processed.plan, dict)
            self.assertIn("decision_gate", processed.plan)
            self.assertTrue(processed.plan["decision_gate"]["triggered"])
            stage_updates = processed.plan["stage_updates"]
            self.assertEqual([entry["stage"] for entry in stage_updates], ["decision_gate_required"])
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
            self.assertTrue(retry_enqueue.enqueued)

    def test_decision_gate_notification_includes_reply_components(self) -> None:
        run_id = self._queue_run(
            "TP-399",
            issue_summary="Unclear requirements",
            issue_description="TBD: need to decide later?",
        )

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
        self.assertTrue(kwargs["open_thread"])
        self.assertIsInstance(kwargs["thread_intro_components"], list)
        self.assertEqual(kwargs["thread_intro_components"][0]["components"][0]["custom_id"], "ask.reply.open")

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
            lock = session.get(RunLock, {"tenant_id": "tenant-worker", "issue_key": "ZZ-101"})
            self.assertIsNone(lock)

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
            stage_updates = processed.plan["stage_updates"]
            self.assertIn(
                "https://jira.example.test/browse/TP-555",
                stage_updates[0]["discord_message"],
            )
