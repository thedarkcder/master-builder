from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from orchestrator.core.worker.workflow_request_service import build_workflow_request_for_run


class WorkflowRequestServiceTests(unittest.TestCase):
    def _base_inputs(self, checkout_base_dir: str) -> tuple[SimpleNamespace, SimpleNamespace, dict, SimpleNamespace]:
        tenant = SimpleNamespace(tenant_id="tenant-1")
        run = SimpleNamespace(
            run_id="run-1",
            issue_key="TP-1",
            issue_summary="Summary",
            issue_description="Description",
            project_id="project-1",
        )
        effective_policy = {"max_dev_test_review_loops": 1, "allowed_commands": []}
        settings = SimpleNamespace(project_repo_checkout_base_dir=checkout_base_dir)
        return tenant, run, effective_policy, settings

    def test_build_workflow_request_requires_project_context(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            with self.assertRaisesRegex(ValueError, "project routing is required"):
                build_workflow_request_for_run(
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
            )
            with self.assertRaisesRegex(ValueError, "checkout is missing"):
                build_workflow_request_for_run(
                    tenant=tenant,
                    run=run,
                    project=project,
                    effective_policy=effective_policy,
                    settings=settings,
                )

    def test_build_workflow_request_uses_project_checkout_dir(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tenant, run, effective_policy, settings = self._base_inputs(tmp_dir)
            project = SimpleNamespace(
                project_id="project-1",
                name="Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
            )
            checkout_dir = Path(tmp_dir) / "tenant-1" / "project-1" / "repo"
            checkout_dir.mkdir(parents=True, exist_ok=True)
            request = build_workflow_request_for_run(
                tenant=tenant,
                run=run,
                project=project,
                effective_policy=effective_policy,
                settings=settings,
            )
            self.assertEqual(request.execution_repo_dir, str(checkout_dir))


if __name__ == "__main__":
    unittest.main()
