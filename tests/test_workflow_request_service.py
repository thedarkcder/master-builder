from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from orchestrator.core.worker.capability_normalization import WorkerCapability
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import PmPlan, WorkflowStageCheckpoint
from orchestrator.core.worker.workflow_request_service import build_workflow_request_for_run
from orchestrator.tools.github_app import PullRequestSummary


class WorkflowRequestServiceTests(unittest.TestCase):
    @staticmethod
    def _plan_with_trigger_context(trigger_context: dict) -> dict:
        return ExecutionSnapshot.empty(trigger_context=trigger_context).dump()

    @staticmethod
    def _checkpoint_payload_with_pm_plan() -> dict:
        snapshot = ExecutionSnapshot.empty()
        snapshot.apply_stage_checkpoint(
            WorkflowStageCheckpoint(
                stage="pm",
                attempt=1,
                status="completed",
                summary="PM ready",
                plan=PmPlan(
                    plan_steps=["restore auth flow"],
                    acceptance_criteria=["login works"],
                    risks=[],
                ),
            )
        )
        return snapshot.dump()

    def _session_with_no_human_inputs(self) -> SimpleNamespace:
        return SimpleNamespace(
            execute=lambda *_args, **_kwargs: SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [])
            )
        )

    def _base_inputs(self, checkout_base_dir: str) -> tuple[SimpleNamespace, SimpleNamespace, dict, SimpleNamespace]:
        tenant = SimpleNamespace(tenant_id="tenant-1")
        run = SimpleNamespace(
            run_id="run-1",
            workflow_id="workflow-1",
            attempt_number=1,
            issue_key="TP-1",
            issue_summary="Summary",
            issue_description="Description",
            project_id="project-1",
            branch=None,
            plan=None,
            entry_mode="fresh",
            entry_stage=None,
            entry_checkpoint_id=None,
        )
        effective_policy = {"max_dev_test_review_loops": 1, "allowed_commands": [], "allow_pr_creation": True}
        settings = SimpleNamespace(
            project_repo_checkout_base_dir=checkout_base_dir,
            worker_capabilities="linux",
            worker_workspace_key="worker-a",
        )
        return tenant, run, effective_policy, settings

    @staticmethod
    def _prepared_repo(checkout_dir: Path, *, start_point_ref: str = "origin/main") -> SimpleNamespace:
        return SimpleNamespace(
            prepared_repo=SimpleNamespace(
                repo_dir=checkout_dir,
                execution_branch="run/tp-1/run-1",
                start_point_ref=start_point_ref,
                start_point_sha="abc123",
                workspace_key="worker-a",
            ),
            actions_taken=(),
        )

    def test_build_workflow_request_requires_project_context(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            with self.assertRaisesRegex(ValueError, "project routing is required"):
                build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=None,
                    effective_policy=effective_policy,
                    settings=settings,
                )

    def test_build_workflow_request_rejects_invalid_worker_capability_settings(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            settings.worker_capabilities = "linux,darwin"
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)
            with patch(
                "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                return_value=self._prepared_repo(checkout_dir),
            ):
                with self.assertRaisesRegex(ValueError, "Invalid worker capability token\\(s\\)"):
                    build_workflow_request_for_run(
                        session=self._session_with_no_human_inputs(),
                        tenant=tenant,
                        run=run,
                        project=project,
                        effective_policy=effective_policy,
                        settings=settings,
                    )

    def test_build_workflow_request_requires_existing_checkout_dir(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            with patch(
                "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                side_effect=ValueError("checkout is missing"),
            ):
                with self.assertRaisesRegex(ValueError, "Run repo setup failed"):
                    build_workflow_request_for_run(
                        session=self._session_with_no_human_inputs(),
                        tenant=tenant,
                        run=run,
                        project=project,
                        effective_policy=effective_policy,
                        settings=settings,
                    )

    def test_build_workflow_request_wraps_worktree_bootstrap_errors(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            with patch(
                "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                side_effect=NotADirectoryError("repo/.git/info"),
            ):
                with self.assertRaisesRegex(ValueError, "Run repo setup failed"):
                    build_workflow_request_for_run(
                        session=self._session_with_no_human_inputs(),
                        tenant=tenant,
                        run=run,
                        project=project,
                        effective_policy=effective_policy,
                        settings=settings,
                    )

    def test_build_workflow_request_uses_run_worktree_dir(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)
            with patch(
                "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                return_value=self._prepared_repo(checkout_dir),
            ):
                request = build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.execution_repo_dir, str(checkout_dir))
            self.assertEqual(request.execution_branch, "run/tp-1/run-1")
            self.assertEqual(request.workspace_key, "worker-a")
            self.assertEqual(request.start_point_ref, "origin/main")
            self.assertEqual(request.start_point_sha, "abc123")
            self.assertEqual(request.current_worker_capability, WorkerCapability.LINUX)
            self.assertEqual(request.available_worker_capabilities, (WorkerCapability.LINUX,))
            self.assertEqual(request.project_id, "project-1")
            self.assertEqual(request.project_name, "Project")
            self.assertEqual(request.github_repository, "https://github.com/example/repo")
            self.assertEqual(request.jira_project_key, "TP")
            self.assertTrue(request.allow_pr_creation)
            self.assertEqual(request.integration_branch, "feature/TP-1")
            self.assertEqual(run.branch, "feature/TP-1")

    def test_build_workflow_request_resolves_missing_project_branch_from_github_default(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            tenant.github_config = {"installation_id": "12345"}
            settings.secrets_encryption_key = ""
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)

            class _FakeGitHubClient:
                def get_repository_default_branch(self, *, repo_full_name: str, github_repository: str) -> str:
                    self.default_branch_args = (repo_full_name, github_repository)
                    return "master"

                def list_open_pull_requests(self, *, repo_full_name: str, limit: int = 20):  # noqa: ANN001
                    self.open_pr_args = (repo_full_name, limit)
                    return []

            fake_client = _FakeGitHubClient()
            prepared_base_branches: list[str] = []

            def _prepare_execution_repo_for_run(**kwargs):  # noqa: ANN003, ANN202
                prepared_base_branches.append(kwargs["base_branch"])
                return self._prepared_repo(checkout_dir, start_point_ref="origin/master")

            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.github_client_from_tenant_config",
                    return_value=fake_client,
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                    side_effect=_prepare_execution_repo_for_run,
                ),
            ):
                request = build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(fake_client.default_branch_args, ("example/repo", "https://github.com/example/repo"))
            self.assertEqual(prepared_base_branches, ["master"])
            self.assertEqual(request.base_branch, "master")
            self.assertEqual(request.pr_target_branch, "master")
            self.assertEqual(request.start_point_ref, "origin/master")

    def test_build_workflow_request_reuses_existing_open_pr_branch(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            tenant.github_config = {"installation_id": "12345"}
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)

            class _FakeGitHubClient:
                def list_open_pull_requests(self, *, repo_full_name: str, limit: int = 20):  # noqa: ANN001
                    self.last_repo_full_name = repo_full_name
                    self.last_limit = limit
                    return [
                        PullRequestSummary(
                            number=99,
                            title="GP-999 unrelated",
                            state="open",
                            html_url="https://github.com/example/repo/pull/99",
                            head_ref="feature/GP-999",
                            base_ref="main",
                            updated_at="2026-03-15T12:00:00Z",
                        ),
                        PullRequestSummary(
                            number=77,
                            title="TP-1 improve auth retries",
                            state="open",
                            html_url="https://github.com/example/repo/pull/77",
                            head_ref="feature/TP-1-shared",
                            base_ref="main",
                            updated_at="2026-03-15T10:00:00Z",
                        ),
                    ]

            fake_client = _FakeGitHubClient()
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.github_client_from_tenant_config",
                    return_value=fake_client,
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                    return_value=self._prepared_repo(checkout_dir),
                ),
            ):
                request = build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.integration_branch, "feature/TP-1-shared")
            self.assertEqual(run.branch, "feature/TP-1-shared")

    def test_build_workflow_request_extracts_checkpoint_resume_metadata(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.entry_mode = "resume"
            run.entry_stage = "dev"
            run.entry_checkpoint_id = "checkpoint-1"
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                    return_value=self._prepared_repo(checkout_dir),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service._entry_checkpoint",
                    return_value=SimpleNamespace(
                        checkpoint_id="checkpoint-1",
                        checkpoint_kind="execution",
                        payload_json=self._checkpoint_payload_with_pm_plan(),
                        codex_session_id="dev-session-123",
                    ),
                ),
            ):
                request = build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.entry_mode, "resume")
            self.assertEqual(request.entry_stage, "dev")
            self.assertEqual(request.checkpoint_kind, "execution")
            self.assertEqual(request.checkpoint_id, "checkpoint-1")
            self.assertEqual(request.checkpoint_session_id, "dev-session-123")
            self.assertEqual(
                request.checkpoint_payload["stages"]["pm"]["artifact"]["plan_steps"],
                ["restore auth flow"],
            )

    def test_build_workflow_request_extracts_review_resume_metadata(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.entry_mode = "resume"
            run.entry_stage = "review"
            run.entry_checkpoint_id = "checkpoint-1"
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                    return_value=self._prepared_repo(checkout_dir),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service._entry_checkpoint",
                    return_value=SimpleNamespace(
                        checkpoint_id="checkpoint-1",
                        checkpoint_kind="execution",
                        payload_json=self._checkpoint_payload_with_pm_plan(),
                        codex_session_id="dev-session-123",
                    ),
                ),
            ):
                request = build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.entry_mode, "resume")
            self.assertEqual(request.entry_stage, "review")
            self.assertEqual(request.checkpoint_kind, "execution")
            self.assertEqual(request.checkpoint_session_id, "dev-session-123")
            self.assertEqual(
                request.checkpoint_payload["stages"]["pm"]["artifact"]["plan_steps"],
                ["restore auth flow"],
            )

    def test_build_workflow_request_rejects_unsupported_resume_checkpoint_payload(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.entry_mode = "resume"
            run.entry_stage = "dev"
            run.entry_checkpoint_id = "checkpoint-1"
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                    return_value=self._prepared_repo(checkout_dir),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service._entry_checkpoint",
                    return_value=SimpleNamespace(
                        checkpoint_id="checkpoint-1",
                        checkpoint_kind="execution",
                        payload_json={"version": 999, "context": {}, "workflow": {}, "events": {}, "stages": {}},
                        codex_session_id="dev-session-123",
                    ),
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "Unsupported execution snapshot version/shape",
                ):
                    build_workflow_request_for_run(
                        session=self._session_with_no_human_inputs(),
                        tenant=tenant,
                        run=run,
                        project=project,
                        effective_policy=effective_policy,
                        settings=settings,
                    )

    def test_build_workflow_request_prefers_remediation_trigger_branch_and_base(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.branch = "feature/TP-1-stale"
            run.plan = self._plan_with_trigger_context(
                {
                    "source": "github_pr_review_feedback",
                    "pr_number": 14,
                    "head_ref": "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                    "base_ref": "main",
                }
            )
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "staging"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                    return_value=self._prepared_repo(checkout_dir),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service._resolve_branch_from_open_pull_requests",
                    return_value="feature/tp-1-fallback",
                ) as open_pr_branch_mock,
            ):
                request = build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(
                request.integration_branch,
                "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
            )
            self.assertEqual(request.base_branch, "main")
            self.assertEqual(request.pr_target_branch, "main")
            self.assertEqual(run.branch, "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703")
            open_pr_branch_mock.assert_not_called()

    def test_build_workflow_request_includes_answered_human_inputs(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={"default_branch": "main"},
            )
            checkout_dir = (
                Path(tmp_dir)
                / "tenant-1"
                / "project-1"
                / "runs"
                / "run-1"
                / "workspaces"
                / "worker-a"
                / "repo"
            )
            checkout_dir.mkdir(parents=True, exist_ok=True)
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.prepare_execution_repo_for_run",
                    return_value=self._prepared_repo(checkout_dir),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.answered_human_inputs_for_attempt",
                    return_value=[
                        {
                            "request_id": "request-1",
                            "request_type": "verification_code",
                            "prompt": "Enter the Apple verification code",
                            "value": "123456",
                        }
                    ],
                ),
            ):
                request = build_workflow_request_for_run(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.human_inputs[0]["request_type"], "verification_code")
            self.assertEqual(request.human_inputs[0]["value"], "123456")


if __name__ == "__main__":
    unittest.main()
