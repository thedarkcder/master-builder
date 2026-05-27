from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.worker.repo_setup_service import RetryableRepoSetupError, prepare_execution_repo_for_run
from orchestrator.tools.project_repo_checkout import PreparedExecutionRepo, ProjectRepoCheckoutError


def _repo_setup_subject(
    *,
    checkout_base_dir: str,
    payload: dict[str, object] | list[dict[str, object]],
) -> dict[str, object]:
    payloads = list(payload) if isinstance(payload, list) else [payload]

    def _invoke_json(**_kwargs):  # noqa: ANN001
        if len(payloads) > 1:
            return payloads.pop(0)
        return payloads[0]

    tenant = SimpleNamespace(tenant_id="tenant-a")
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
    settings = SimpleNamespace(project_repo_checkout_base_dir=checkout_base_dir)
    stage_session = SimpleNamespace(
        tooling=SimpleNamespace(
            governed_prompt_context=lambda: {
                "allowed_tools_json": "[]",
                "tool_catalog_json": "{}",
                "domain_model": {},
            }
        ),
        invoke_json=_invoke_json,
    )
    return {
        "session": object(),
        "settings": settings,
        "tenant": tenant,
        "run": run,
        "project": project,
        "base_branch": "main",
        "integration_branch": "feature/TP-1",
        "workspace_key": "worker-a",
        "stage_session": stage_session,
        "payloads": payloads,
    }


def test_prepare_execution_repo_treats_final_validation_failure_as_retryable(tmp_path: Path) -> None:
    repo_dir = tmp_path / "tenant-a" / "project-a" / "runs" / "run-1" / "workspaces" / "worker-a" / "repo"
    subject = _repo_setup_subject(
        checkout_base_dir=str(tmp_path),
        payload={
            "outcome": "ready",
            "execution_repo_dir": str(repo_dir),
            "execution_branch": "run/tp-1/run-1",
            "workspace_key": "worker-a",
            "actions_taken": ["model prepared repo"],
        },
    )

    with (
        patch("orchestrator.core.worker.repo_setup_service.resolve_execution_profile_for_selector", return_value=object()),
        patch("orchestrator.core.worker.repo_setup_service.build_runtime_for_execution_profile", return_value=object()),
        patch(
            "orchestrator.core.worker.repo_setup_service.RuntimeStageSession.create",
            return_value=subject["stage_session"],
        ),
        patch(
            "orchestrator.core.worker.repo_setup_service.validate_execution_repo",
            side_effect=ProjectRepoCheckoutError("Execution repo metadata is missing at /repo"),
        ),
    ):
        with pytest.raises(RetryableRepoSetupError, match="Repo setup validation failed"):
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


def test_prepare_execution_repo_repairs_validation_failure_before_requeue(tmp_path: Path) -> None:
    first_repo_dir = tmp_path / "tenant-a" / "project-a" / "runs" / "run-1" / "workspaces" / "worker-a" / "repo"
    repaired_repo_dir = tmp_path / "tenant-a" / "project-a" / "runs" / "run-1" / "workspaces" / "worker-a" / "repo-repaired"
    subject = _repo_setup_subject(
        checkout_base_dir=str(tmp_path),
        payload=[
            {
                "outcome": "ready",
                "execution_repo_dir": str(first_repo_dir),
                "execution_branch": "run/tp-1/run-1",
                "workspace_key": "worker-a",
                "actions_taken": ["model prepared repo"],
            },
            {
                "outcome": "ready",
                "execution_repo_dir": str(repaired_repo_dir),
                "execution_branch": "run/tp-1/run-1",
                "workspace_key": "worker-a",
                "actions_taken": ["model repaired metadata"],
            },
        ],
    )
    prepared_repo = PreparedExecutionRepo(
        repo_dir=repaired_repo_dir,
        execution_branch="run/tp-1/run-1",
        workspace_key="worker-a",
        start_point_ref="main",
        start_point_sha="abc123",
        repo_kind="run_worktree",
    )

    with (
        patch("orchestrator.core.worker.repo_setup_service.resolve_execution_profile_for_selector", return_value=object()),
        patch("orchestrator.core.worker.repo_setup_service.build_runtime_for_execution_profile", return_value=object()),
        patch(
            "orchestrator.core.worker.repo_setup_service.RuntimeStageSession.create",
            return_value=subject["stage_session"],
        ),
        patch(
            "orchestrator.core.worker.repo_setup_service.validate_execution_repo",
            side_effect=[
                ProjectRepoCheckoutError("Execution repo metadata is missing at /repo"),
                prepared_repo,
            ],
        ) as validate_execution_repo,
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
    assert result.actions_taken == ("model repaired metadata",)
    assert validate_execution_repo.call_count == 2
