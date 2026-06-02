from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.worker.repo_setup_service import (
    RetryableRepoSetupError,
    TerminalRepoSetupError,
    prepare_execution_repo_for_run,
)
from orchestrator.tools.project_repo_checkout import PreparedExecutionRepo, ProjectRepoCheckoutError


def _repo_setup_subject(*, checkout_base_dir: str) -> dict[str, object]:
    tenant = SimpleNamespace(tenant_id="tenant-a", github_config={})
    project = SimpleNamespace(
        project_id="project-a",
        name="Project A",
        github_repository="https://github.com/example/repo",
    )
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="workflow-1",
        issue_key="TP-1",
        issue_summary="Repo setup",
        issue_description="Prepare execution repo",
        attempt_number=1,
    )
    settings = SimpleNamespace(project_repo_checkout_base_dir=checkout_base_dir, secrets_encryption_key="secret")
    return {
        "session": object(),
        "settings": settings,
        "tenant": tenant,
        "run": run,
        "project": project,
        "base_branch": "main",
        "integration_branch": "feature/TP-1",
        "workspace_key": "worker-a",
    }


def test_prepare_execution_repo_uses_deterministic_checkout_helpers(tmp_path: Path) -> None:
    subject = _repo_setup_subject(checkout_base_dir=str(tmp_path))
    prepared_repo = PreparedExecutionRepo(
        repo_dir=tmp_path / "tenant-a" / "project-a" / "runs" / "run-1" / "workspaces" / "worker-a" / "repo",
        execution_branch="run/tp-1/run-1",
        workspace_key="worker-a",
        start_point_ref="origin/main",
        start_point_sha="abc123",
        repo_kind="run_worktree",
    )

    with (
        patch("orchestrator.core.worker.repo_setup_service._github_installation_token_for_project", return_value="token") as token_mock,
        patch("orchestrator.core.worker.repo_setup_service.ensure_project_checkout") as ensure_project_checkout_mock,
        patch(
            "orchestrator.core.worker.repo_setup_service.ensure_run_worktree",
            return_value=(prepared_repo.repo_dir, prepared_repo.execution_branch),
        ) as ensure_run_worktree_mock,
        patch("orchestrator.core.worker.repo_setup_service.validate_execution_repo", return_value=prepared_repo) as validate_mock,
    ):
        result = prepare_execution_repo_for_run(
            session=subject["session"],
            settings=subject["settings"],
            tenant=subject["tenant"],
            run=subject["run"],
            project=subject["project"],
            base_branch=subject["base_branch"],
            integration_branch=subject["integration_branch"],
            workspace_key=subject["workspace_key"],
        )

    assert result.prepared_repo == prepared_repo
    assert result.actions_taken == (
        "Ensured shared repository checkout is present and current",
        "Ensured execution worktree exists on the run branch with execution metadata",
        "Validated execution repository metadata, branch, and cleanliness",
    )
    token_mock.assert_called_once()
    ensure_project_checkout_mock.assert_called_once()
    ensure_run_worktree_mock.assert_called_once()
    validate_mock.assert_called_once()


def test_prepare_execution_repo_treats_worktree_validation_failure_as_retryable(tmp_path: Path) -> None:
    subject = _repo_setup_subject(checkout_base_dir=str(tmp_path))

    with (
        patch("orchestrator.core.worker.repo_setup_service._github_installation_token_for_project", return_value="token"),
        patch("orchestrator.core.worker.repo_setup_service.ensure_project_checkout"),
        patch(
            "orchestrator.core.worker.repo_setup_service.ensure_run_worktree",
            side_effect=ProjectRepoCheckoutError("Execution repo metadata is missing"),
        ),
    ):
        with pytest.raises(RetryableRepoSetupError, match="Execution repo metadata is missing"):
            prepare_execution_repo_for_run(
                session=subject["session"],
                settings=subject["settings"],
                tenant=subject["tenant"],
                run=subject["run"],
                project=subject["project"],
                base_branch=subject["base_branch"],
                integration_branch=subject["integration_branch"],
                workspace_key=subject["workspace_key"],
            )


def test_prepare_execution_repo_treats_missing_credentials_as_terminal(tmp_path: Path) -> None:
    subject = _repo_setup_subject(checkout_base_dir=str(tmp_path))

    with patch(
        "orchestrator.core.worker.repo_setup_service._github_installation_token_for_project",
        side_effect=ValueError("missing private key"),
    ):
        with pytest.raises(TerminalRepoSetupError, match="Repository checkout credentials are unavailable"):
            prepare_execution_repo_for_run(
                session=subject["session"],
                settings=subject["settings"],
                tenant=subject["tenant"],
                run=subject["run"],
                project=subject["project"],
                base_branch=subject["base_branch"],
                integration_branch=subject["integration_branch"],
                workspace_key=subject["workspace_key"],
            )
