from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from orchestrator.core.worker.workflow_request_service import build_workflow_request_for_run
from orchestrator.tools.github_app import PullRequestSummary


class WorkflowRequestServiceTests(unittest.TestCase):
    def _base_inputs(self, checkout_base_dir: str) -> tuple[SimpleNamespace, SimpleNamespace, dict, SimpleNamespace]:
        tenant = SimpleNamespace(tenant_id="tenant-1")
        run = SimpleNamespace(
            run_id="run-1",
            issue_key="TP-1",
            issue_summary="Summary",
            issue_description="Description",
            project_id="project-1",
            branch=None,
            plan=None,
        )
        effective_policy = {"max_dev_test_review_loops": 1, "allowed_commands": [], "allow_pr_creation": True}
        settings = SimpleNamespace(
            project_repo_checkout_base_dir=checkout_base_dir,
            worker_capabilities="linux",
            worker_workspace_key="worker-a",
        )
        return tenant, run, effective_policy, settings

    def test_build_workflow_request_requires_project_context(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            with self.assertRaisesRegex(ValueError, "project routing is required"):
                build_workflow_request_for_run(
                    session=SimpleNamespace(),
                    tenant=tenant,
                    run=run,
                    project=None,
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
                environment={},
            )
            with patch(
                "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                side_effect=ValueError("checkout is missing"),
            ):
                with self.assertRaisesRegex(ValueError, "checkout is missing"):
                    build_workflow_request_for_run(
                        session=SimpleNamespace(),
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
                environment={},
            )
            with patch(
                "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                side_effect=NotADirectoryError("repo/.git/info"),
            ):
                with self.assertRaisesRegex(ValueError, "Run worktree bootstrap failed"):
                    build_workflow_request_for_run(
                        session=SimpleNamespace(),
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
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                    return_value=(checkout_dir, "run/tp-1/run-1"),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.read_run_worktree_metadata",
                    return_value={"start_point_ref": "origin/main", "start_point_sha": "abc123"},
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.validate_run_worktree",
                    return_value=None,
                ),
            ):
                request = build_workflow_request_for_run(
                    session=SimpleNamespace(),
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
            self.assertEqual(request.current_worker_capability, "linux")
            self.assertEqual(request.available_worker_capabilities, ["linux"])
            self.assertEqual(request.project_id, "project-1")
            self.assertEqual(request.project_name, "Project")
            self.assertEqual(request.github_repository, "https://github.com/example/repo")
            self.assertEqual(request.jira_project_key, "TP")
            self.assertTrue(request.allow_pr_creation)
            self.assertEqual(request.integration_branch, "feature/TP-1")
            self.assertEqual(run.branch, "feature/TP-1")

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
                    "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                    return_value=(checkout_dir, "run/tp-1/run-1"),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.read_run_worktree_metadata",
                    return_value={"start_point_ref": "origin/main", "start_point_sha": "abc123"},
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.validate_run_worktree",
                    return_value=None,
                ),
            ):
                request = build_workflow_request_for_run(
                    session=SimpleNamespace(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.integration_branch, "feature/TP-1-shared")
            self.assertEqual(run.branch, "feature/TP-1-shared")

    def test_build_workflow_request_extracts_resume_metadata(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.plan = {
                "trigger_context": {
                    "rerun_mode": "resume",
                    "resume_stage": "dev",
                    "resume_session_id": "dev-session-123",
                    "resume_source_plan": {
                        "plan_steps": ["restore auth flow"],
                        "acceptance_criteria": ["login works"],
                        "risks": [],
                    },
                }
            }
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
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                    return_value=(checkout_dir, "run/tp-1/run-1"),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.read_run_worktree_metadata",
                    return_value={"start_point_ref": "origin/main", "start_point_sha": "abc123"},
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.validate_run_worktree",
                    return_value=None,
                ),
            ):
                request = build_workflow_request_for_run(
                    session=SimpleNamespace(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.resume_mode, "resume")
            self.assertEqual(request.resume_stage, "dev")
            self.assertEqual(request.resume_session_id, "dev-session-123")
            self.assertEqual(request.resume_source_plan, run.plan["trigger_context"]["resume_source_plan"])

    def test_build_workflow_request_extracts_review_resume_metadata(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.plan = {
                "trigger_context": {
                    "rerun_mode": "resume",
                    "resume_stage": "review",
                    "resume_session_id": "dev-session-123",
                    "resume_source_plan": {
                        "plan_steps": ["restore auth flow"],
                        "acceptance_criteria": ["login works"],
                        "risks": [],
                    },
                }
            }
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
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                    return_value=(checkout_dir, "run/tp-1/run-1"),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.read_run_worktree_metadata",
                    return_value={"start_point_ref": "origin/main", "start_point_sha": "abc123"},
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.validate_run_worktree",
                    return_value=None,
                ),
            ):
                request = build_workflow_request_for_run(
                    session=SimpleNamespace(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

            self.assertEqual(request.resume_mode, "resume")
            self.assertEqual(request.resume_stage, "review")
            self.assertEqual(request.resume_session_id, "dev-session-123")
            self.assertEqual(request.resume_source_plan, run.plan["trigger_context"]["resume_source_plan"])

    def test_build_workflow_request_prefers_remediation_trigger_branch_and_base(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.branch = "feature/TP-1-stale"
            run.plan = {
                "trigger_context": {
                    "source": "github_pr_review_feedback",
                    "pr_number": 14,
                    "head_ref": "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                    "base_ref": "main",
                }
            }
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
                    "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                    return_value=(checkout_dir, "run/tp-1/run-1"),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.read_run_worktree_metadata",
                    return_value={"start_point_ref": "origin/main", "start_point_sha": "abc123"},
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.validate_run_worktree",
                    return_value=None,
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service._resolve_branch_from_open_pull_requests",
                    return_value="feature/tp-1-fallback",
                ) as open_pr_branch_mock,
            ):
                request = build_workflow_request_for_run(
                    session=SimpleNamespace(),
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
            run.plan = {"trigger_context": {"human_input_request_ids": ["request-1"]}}
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
            with (
                patch(
                    "orchestrator.core.worker.workflow_request_service.ensure_run_worktree",
                    return_value=(checkout_dir, "run/tp-1/run-1"),
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.read_run_worktree_metadata",
                    return_value={"start_point_ref": "origin/main", "start_point_sha": "abc123"},
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.validate_run_worktree",
                    return_value=None,
                ),
                patch(
                    "orchestrator.core.worker.workflow_request_service.answered_human_inputs_for_request",
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
                    session=SimpleNamespace(),
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
