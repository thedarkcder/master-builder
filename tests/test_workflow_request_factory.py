from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from orchestrator.core.worker.workflow_request_factory import build_workflow_request
from orchestrator.core.worker.capability_normalization import WorkerCapability


class WorkflowRequestFactoryTests(unittest.TestCase):
    def _session_with_no_human_inputs(self) -> SimpleNamespace:
        return SimpleNamespace(
            execute=lambda *_args, **_kwargs: SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [])
            )
        )

    def _session_with_project_apps(self, apps: list[SimpleNamespace]) -> SimpleNamespace:
        return SimpleNamespace(
            execute=lambda *_args, **_kwargs: SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: apps)
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
    def _prepared_repo(checkout_dir: Path) -> SimpleNamespace:
        return SimpleNamespace(
            prepared_repo=SimpleNamespace(
                repo_dir=checkout_dir,
                execution_branch="run/tp-1/run-1",
                start_point_ref="origin/main",
                start_point_sha="abc123",
                workspace_key="worker-a",
            ),
            actions_taken=(),
        )

    def test_build_requires_project_context(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            with self.assertRaisesRegex(ValueError, "project routing is required"):
                build_workflow_request(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=None,
                    effective_policy=effective_policy,
                    settings=settings,
                )

    def test_build_requires_explicit_loop_policy(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, _effective_policy, settings = self._base_inputs(tmp_dir)
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={},
            )
            with self.assertRaisesRegex(
                ValueError,
                "max_dev_test_review_loops must be a positive integer",
            ):
                build_workflow_request(
                    session=self._session_with_no_human_inputs(),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy={"allowed_commands": [], "allow_pr_creation": True},
                    settings=settings,
                )

    def test_build_uses_prepared_repo(self) -> None:
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
            with patch(
                "orchestrator.core.worker.workflow_request_factory.prepare_execution_repo_for_run",
                return_value=self._prepared_repo(checkout_dir),
            ):
                request = build_workflow_request(
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

    def test_build_derives_project_demo_capture_targets_from_project_apps(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                environment={},
            )
            checkout_dir = Path(tmp_dir) / "repo"
            checkout_dir.mkdir(parents=True, exist_ok=True)
            apps = [
                SimpleNamespace(
                    detected_runtime="nextjs",
                    detected_language="typescript",
                    build_strategy="nixpacks",
                    name="Web app",
                    source_path="web",
                    deployment_config={"services": [{"kind": "website"}]},
                ),
                SimpleNamespace(
                    detected_runtime="swiftui",
                    detected_language="swift",
                    build_strategy="xcode",
                    name="Yana iOS",
                    source_path="ios",
                    deployment_config={},
                ),
                SimpleNamespace(
                    detected_runtime="android",
                    detected_language="kotlin",
                    build_strategy="gradle",
                    name="Yana Android",
                    source_path="android",
                    deployment_config={},
                ),
            ]

            with patch(
                "orchestrator.core.worker.workflow_request_factory.prepare_execution_repo_for_run",
                return_value=self._prepared_repo(checkout_dir),
            ):
                request = build_workflow_request(
                    session=self._session_with_project_apps(apps),
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                    answered_human_inputs_for_attempt_fn=lambda **_: [],
                )

            self.assertEqual(request.project_demo_capture_targets, ("browser", "ios", "android"))

    def test_build_rejects_non_canonical_resume_checkpoint_payload(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            run.entry_mode = "resume"
            run.entry_stage = "pm"
            run.entry_checkpoint_id = "checkpoint-1"
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
            checkpoint = SimpleNamespace(
                checkpoint_id="checkpoint-1",
                checkpoint_kind="pm",
                payload_json={},
                codex_session_id=None,
            )
            with patch(
                "orchestrator.core.worker.workflow_request_factory.prepare_execution_repo_for_run",
                return_value=self._prepared_repo(checkout_dir),
            ):
                with self.assertRaisesRegex(ValueError, "Unsupported execution snapshot version/shape"):
                    build_workflow_request(
                        session=self._session_with_no_human_inputs(),
                        tenant=tenant,
                        run=run,
                        project=project,
                        effective_policy=effective_policy,
                        settings=settings,
                        entry_checkpoint_fn=lambda **_: checkpoint,
                    )
