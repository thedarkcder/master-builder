from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import orchestrator.core.worker.execution_service as execution_service_module
import orchestrator.worker as worker_module
from orchestrator.core.config import get_settings
from orchestrator.core.worker.run_health import worker_service_instance_id_for_mode
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import WorkflowRequest, WorkflowResult, WorkflowRunner
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import Project, Run, Tenant, WorkflowExecution
from tests.test_support.db_harness import SqliteTemplateDbTestCase
from tests.workflow_test_support import add_workflow_attempt

FAILED_RUN_ID = "f424eee4-d8aa-4055-87d0-9fa51dde1e80"
FAILED_WORKFLOW_ID = "f77a6bf8-d297-4a21-a431-67e71b0e6748"
FAILED_TENANT_ID = "route25"
FAILED_PROJECT_ID = "route25-default"
FAILED_ISSUE_KEY = "GP-186"
FAILED_ISSUE_SUMMARY = "GP-186: PR remediation for #26"
FAILED_HEAD_SHA = "809b7c33c2446563bf429009ca1cc7a16a8b5a69"
FAILED_PR_URL = "https://github.com/thedarkcder/girl-power/pull/26"
FAILED_REPO_URL = "https://github.com/thedarkcder/girl-power"


class _FakeHeartbeatController:
    def __init__(self, *args, **kwargs) -> None:  # noqa: D401, ANN002, ANN003
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class _FakeAgents:
    def __init__(self, *, outcome: str) -> None:
        self._outcome = outcome

    def execute(self, request, *, test_feedback_hook=None, stage_checkpoint_hook=None):  # noqa: ANN001
        if self._outcome == "success":
            return WorkflowResult(
                outcome="success",
                plan=None,
                pr_url=None,
                summary=["Simulated success"],
                test_guidance=[],
                attempts=1,
            )
        if self._outcome == "failed":
            return WorkflowResult(
                outcome="failed",
                plan=None,
                pr_url=None,
                summary=["Simulated failure"],
                test_guidance=[],
                attempts=1,
                blocker_message="Simulated agent failure",
            )
        raise AssertionError(f"Unsupported fake outcome {self._outcome}")


class WorkerClaimedRunIntegrationTests(SqliteTemplateDbTestCase):
    @classmethod
    def bootstrap_template_database(cls) -> None:
        session_factory = create_session_factory(database_url=cls._template_database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id=FAILED_TENANT_ID,
                    name="Route25",
                    is_enabled=True,
                    jira_config={"project_keys": ["GP"]},
                    github_config={},
                    repos_config={"github_repository": FAILED_REPO_URL},
                    policy_config={},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id=FAILED_PROJECT_ID,
                    tenant_id=FAILED_TENANT_ID,
                    name="route25-default",
                    github_repository=FAILED_REPO_URL,
                    jira_project_key="GP",
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def setUp(self) -> None:
        self._original_database_url = os.environ.get("ORCHESTRATOR_DATABASE_URL")
        self._original_allow_sqlite = os.environ.get("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS")
        self._original_agent_id = os.environ.get("ORCHESTRATOR_AGENT_ID")
        self._original_worker_caps = os.environ.get("ORCHESTRATOR_WORKER_CAPABILITIES")
        self._original_workspace_key = os.environ.get("ORCHESTRATOR_WORKER_WORKSPACE_KEY")
        self._original_checkout_base_dir = os.environ.get("ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR")
        self.database_url = self._prepare_test_database(name_prefix="worker-claimed-run")
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = "true"
        os.environ["ORCHESTRATOR_AGENT_ID"] = "worker-linux-local"
        os.environ["ORCHESTRATOR_WORKER_CAPABILITIES"] = "linux"
        os.environ["ORCHESTRATOR_WORKER_WORKSPACE_KEY"] = "test-worker"
        os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = self._db_workspace.name
        get_settings.cache_clear()
        reset_db_engine_cache()
        self.session_factory = create_session_factory(database_url=self.database_url)

    def tearDown(self) -> None:
        self._cleanup_test_database()
        if self._original_database_url is None:
            os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        else:
            os.environ["ORCHESTRATOR_DATABASE_URL"] = self._original_database_url
        if self._original_allow_sqlite is None:
            os.environ.pop("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS", None)
        else:
            os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = self._original_allow_sqlite
        if self._original_agent_id is None:
            os.environ.pop("ORCHESTRATOR_AGENT_ID", None)
        else:
            os.environ["ORCHESTRATOR_AGENT_ID"] = self._original_agent_id
        if self._original_worker_caps is None:
            os.environ.pop("ORCHESTRATOR_WORKER_CAPABILITIES", None)
        else:
            os.environ["ORCHESTRATOR_WORKER_CAPABILITIES"] = self._original_worker_caps
        if self._original_workspace_key is None:
            os.environ.pop("ORCHESTRATOR_WORKER_WORKSPACE_KEY", None)
        else:
            os.environ["ORCHESTRATOR_WORKER_WORKSPACE_KEY"] = self._original_workspace_key
        if self._original_checkout_base_dir is None:
            os.environ.pop("ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR", None)
        else:
            os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = self._original_checkout_base_dir
        get_settings.cache_clear()
        reset_db_engine_cache()

    @staticmethod
    def _failed_run_snapshot() -> dict[str, object]:
        snapshot = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "event": "check_run",
                "action": "completed",
                "pr_number": 26,
                "pr_url": FAILED_PR_URL,
                "head_sha": FAILED_HEAD_SHA,
                "head_ref": "feature/GP-186",
                "base_ref": "main",
                "issue_key": FAILED_ISSUE_KEY,
                "issue_created": False,
                "failing_checks": [],
                "changes_requested": [],
                "requested_comment": None,
                "manual_fix_request": None,
                "review_comments": [
                    {
                        "path": "fastlane/Fastfile",
                        "line": 412,
                        "body": "Fastlane runtime logic changed without proving the workflow still pauses and resumes correctly.",
                    }
                ],
                "issue_comments": [
                    {
                        "body": "## Codex PR Review\n\nStatus: BLOCKED\n\nFindings remain around signing flow and missing validation evidence.",
                    }
                ],
            }
        )
        snapshot.apply_execution_context({"pre_check_outcome": "ready_for_agent"})
        return snapshot.dump()

    def _seed_failed_run_regression_fixture(
        self,
        *,
        run_id: str = FAILED_RUN_ID,
        workflow_id: str = FAILED_WORKFLOW_ID,
        issue_key: str = FAILED_ISSUE_KEY,
        issue_summary: str = FAILED_ISSUE_SUMMARY,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                tenant_id=FAILED_TENANT_ID,
                project_id=FAILED_PROJECT_ID,
                issue_key=issue_key,
                issue_summary=issue_summary,
                issue_description=(
                    "Automated remediation run triggered from GitHub PR #26 "
                    f"({FAILED_PR_URL}). Event: check_run/completed. "
                    f"Head SHA: {FAILED_HEAD_SHA}."
                ),
                repo_url=FAILED_REPO_URL,
                workflow_status="queued",
                run_status="queued",
                dedupe_scope="pr_remediation",
                pre_check_outcome="ready_for_agent",
                required_worker_capability="linux",
                plan=self._failed_run_snapshot(),
                entry_mode="fresh",
                entry_stage="orchestrated",
                created_at=now,
            )
            session.commit()

    def _claim_run(self, *, expected_run_id: str):
        settings = get_settings()
        claimed = worker_module._claim_next_run_once(
            session_factory=self.session_factory,
            settings=settings,
            service_instance_id=worker_service_instance_id_for_mode(settings=settings, mode="runs"),
        )
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.run_id, expected_run_id)
        self.assertTrue(claimed.claim_id)
        return claimed

    @staticmethod
    def _fake_workflow_request(*, session, tenant, run, project, effective_policy, settings):  # noqa: ANN001
        return WorkflowRequest(
            tenant_id=tenant.tenant_id,
            workflow_id=run.workflow_id,
            project_id=project.project_id if project is not None else run.project_id,
            project_name=project.name if project is not None else None,
            github_repository=project.github_repository if project is not None else None,
            jira_project_key=project.jira_project_key if project is not None else None,
            run_id=run.run_id,
            attempt_number=int(getattr(run, "attempt_number", 1) or 1),
            issue_key=run.issue_key,
            issue_summary=run.issue_summary,
            issue_description=run.issue_description or "",
            max_dev_test_review_loops=1,
            allow_pr_creation=False,
            suggested_test_commands=[],
            execution_repo_dir="/tmp/fake-repo",
            workspace_key="test-worker",
            current_worker_capability="linux",
            available_worker_capabilities=("linux",),
            base_branch="main",
            integration_branch="feature/GP-186",
            pr_target_branch="main",
            execution_branch="run/gp-186",
            start_point_ref=None,
            start_point_sha=None,
            pr_number=26,
            trigger_context=ExecutionSnapshot.require(run.plan).trigger_context(),
            entry_mode=str(getattr(run, "entry_mode", "fresh") or "fresh"),
            entry_stage=getattr(run, "entry_stage", None),
            checkpoint_kind=None,
            checkpoint_id=None,
            checkpoint_payload=None,
            checkpoint_session_id=None,
            human_inputs=[],
        )

    def _run_claimed(self, *, claimed_run, outcome: str):
        fake_runner = WorkflowRunner(_FakeAgents(outcome=outcome))
        with (
            patch.object(worker_module, "build_workflow_runner_for_session", return_value=fake_runner),
            patch.object(execution_service_module, "_build_workflow_request_for_run", new=self._fake_workflow_request),
            patch.object(execution_service_module, "ensure_project_repository_checkout", side_effect=lambda *args, **kwargs: None),
            patch.object(execution_service_module, "cleanup_run_workspaces", side_effect=lambda *args, **kwargs: None),
            patch.object(
                execution_service_module,
                "send_tenant_discord_message",
                side_effect=lambda *args, **kwargs: SimpleNamespace(sent=False, reason="test"),
            ),
            patch.object(execution_service_module, "_send_stage_update_to_jira", side_effect=lambda *args, **kwargs: None),
            patch.object(execution_service_module, "_transition_issue_status", side_effect=lambda *args, **kwargs: None),
            patch.object(
                execution_service_module,
                "tenant_jira_issue_url",
                side_effect=lambda *args, **kwargs: f"https://jira.example/browse/{kwargs['issue_key']}",
            ),
            patch.object(execution_service_module, "WorkerRunHeartbeatController", _FakeHeartbeatController),
        ):
            return worker_module._process_next_run_once(
                session_factory=self.session_factory,
                claimed_run_id=claimed_run.run_id,
                claim_id=claimed_run.claim_id,
            )

    def _run_claimed_child_entry(self, *, claimed_run, outcome: str) -> int:
        fake_runner = WorkflowRunner(_FakeAgents(outcome=outcome))
        with (
            patch.object(worker_module, "build_workflow_runner_for_session", return_value=fake_runner),
            patch.object(execution_service_module, "_build_workflow_request_for_run", new=self._fake_workflow_request),
            patch.object(execution_service_module, "ensure_project_repository_checkout", side_effect=lambda *args, **kwargs: None),
            patch.object(execution_service_module, "cleanup_run_workspaces", side_effect=lambda *args, **kwargs: None),
            patch.object(
                execution_service_module,
                "send_tenant_discord_message",
                side_effect=lambda *args, **kwargs: SimpleNamespace(sent=False, reason="test"),
            ),
            patch.object(execution_service_module, "_send_stage_update_to_jira", side_effect=lambda *args, **kwargs: None),
            patch.object(execution_service_module, "_transition_issue_status", side_effect=lambda *args, **kwargs: None),
            patch.object(
                execution_service_module,
                "tenant_jira_issue_url",
                side_effect=lambda *args, **kwargs: f"https://jira.example/browse/{kwargs['issue_key']}",
            ),
            patch.object(execution_service_module, "WorkerRunHeartbeatController", _FakeHeartbeatController),
            patch.dict(
                os.environ,
                {
                    "ORCHESTRATOR_WORKER_CLAIMED_RUN_ID": claimed_run.run_id,
                    "ORCHESTRATOR_WORKER_CLAIM_ID": claimed_run.claim_id,
                },
                clear=False,
            ),
        ):
            return worker_module.run_worker_child_once(mode="runs")

    def _load_run(self, *, run_id: str) -> Run:
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            assert run is not None
            session.expunge(run)
            return run

    def _load_workflow(self, *, workflow_id: str) -> WorkflowExecution:
        with self.session_factory() as session:
            workflow = session.get(WorkflowExecution, workflow_id)
            assert workflow is not None
            session.expunge(workflow)
            return workflow

    def test_parent_claim_persists_dispatching_claim_id_and_owner(self) -> None:
        self._seed_failed_run_regression_fixture()

        claimed = self._claim_run(expected_run_id=FAILED_RUN_ID)

        run = self._load_run(run_id=FAILED_RUN_ID)
        self.assertEqual(claimed.run_id, FAILED_RUN_ID)
        self.assertEqual(run.status, "dispatching")
        self.assertEqual(run.claim_id, claimed.claim_id)
        self.assertEqual(run.worker_service_instance_id, "worker-linux-local:runs")
        self.assertIsNotNone(run.dispatch_claimed_at)
        self.assertIsNone(run.started_at)
        self.assertEqual(run.issue_key, FAILED_ISSUE_KEY)
        self.assertEqual(run.issue_summary, FAILED_ISSUE_SUMMARY)
        self.assertEqual(run.dedupe_scope, "pr_remediation")
        self.assertEqual(ExecutionSnapshot.require(run.plan).trigger_context().get("source"), "github_pr_review_feedback")

    def test_failed_live_run_fixture_leaves_dispatching_and_succeeds(self) -> None:
        self._seed_failed_run_regression_fixture()
        claimed = self._claim_run(expected_run_id=FAILED_RUN_ID)

        result = self._run_claimed(claimed_run=claimed, outcome="success")

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "succeeded")
        run = self._load_run(run_id=FAILED_RUN_ID)
        workflow = self._load_workflow(workflow_id=FAILED_WORKFLOW_ID)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(workflow.status, "succeeded")
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertIsNone(run.claim_id)
        self.assertIsNone(run.worker_service_instance_id)
        self.assertNotEqual(run.status, "dispatching")

    def test_failed_live_run_fixture_leaves_dispatching_and_fails(self) -> None:
        self._seed_failed_run_regression_fixture(
            run_id="1c4d7f01-5f5f-4f39-b1e7-5a39b9913d4a",
            workflow_id="41e26fb4-32dd-43f5-afb5-b7d3d8b0ad2c",
        )
        claimed = self._claim_run(expected_run_id="1c4d7f01-5f5f-4f39-b1e7-5a39b9913d4a")

        result = self._run_claimed(claimed_run=claimed, outcome="failed")

        self.assertIsNotNone(result)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.last_error, "Simulated agent failure")
        run = self._load_run(run_id="1c4d7f01-5f5f-4f39-b1e7-5a39b9913d4a")
        workflow = self._load_workflow(workflow_id="41e26fb4-32dd-43f5-afb5-b7d3d8b0ad2c")
        self.assertEqual(run.status, "failed")
        self.assertEqual(workflow.status, "failed")
        self.assertEqual(run.last_error, "Simulated agent failure")
        self.assertIsNotNone(run.started_at)
        self.assertIsNotNone(run.finished_at)
        self.assertIsNone(run.claim_id)
        self.assertIsNone(run.worker_service_instance_id)
        self.assertNotEqual(run.status, "dispatching")

    def test_run_worker_child_entry_leaves_failed_live_run_fixture_dispatching_state(self) -> None:
        self._seed_failed_run_regression_fixture()
        claimed = self._claim_run(expected_run_id=FAILED_RUN_ID)

        exit_code = self._run_claimed_child_entry(claimed_run=claimed, outcome="success")

        self.assertEqual(exit_code, worker_module.WORKER_CHILD_EXIT_PROCESSED)
        run = self._load_run(run_id=FAILED_RUN_ID)
        workflow = self._load_workflow(workflow_id=FAILED_WORKFLOW_ID)
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(workflow.status, "succeeded")
        self.assertNotEqual(run.status, "dispatching")
