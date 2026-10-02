from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.github_app import (
    GitHubApiError,
    github_client_from_tenant_config,
)
from orchestrator.tools.project_repo_checkout import (
    PreparedExecutionRepo,
    ProjectRepoCheckoutError,
    ensure_project_checkout,
    ensure_run_worktree,
    project_checkout_root_dir,
    validate_execution_repo,
)


class RepoSetupError(RuntimeError):
    pass


class RetryableRepoSetupError(RepoSetupError):
    pass


class TerminalRepoSetupError(RepoSetupError):
    pass


@dataclass(frozen=True)
class RepoSetupPreparation:
    prepared_repo: PreparedExecutionRepo
    actions_taken: tuple[str, ...]


def repo_setup_attempt_count_from_plan(plan: object | None) -> int:
    from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot

    snapshot = ExecutionSnapshot.require(plan, allow_empty=True)
    raw_attempts = snapshot.context.execution_context.get("repo_setup_attempts")
    try:
        return max(0, int(raw_attempts))
    except (TypeError, ValueError):
        return 0


def _github_installation_token_for_project(
    *,
    session,
    settings,
    tenant: Tenant,
    project: Project,
) -> str:
    github_config = tenant.github_config or {}
    github_client = github_client_from_tenant_config(
        github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )
    return github_client.get_installation_token()


def prepare_execution_repo_for_run(
    *,
    session,
    settings,
    tenant: Tenant,
    run: Run,
    project: Project,
    base_branch: str,
    integration_branch: str,
    workspace_key: str,
) -> RepoSetupPreparation:
    checkout_root = project_checkout_root_dir(
        base_dir=settings.project_repo_checkout_base_dir,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )
    checkout_root.mkdir(parents=True, exist_ok=True)

    try:
        installation_token = _github_installation_token_for_project(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
        )
    except (GitHubApiError, ProjectRepoCheckoutError, ValueError) as exc:
        raise TerminalRepoSetupError(
            f"Repository checkout credentials are unavailable: {exc}"
        ) from exc

    try:
        ensure_project_checkout(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project=project,
            github_installation_token=installation_token,
        )
        run_repo_dir, execution_branch = ensure_run_worktree(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project=project,
            run_id=run.run_id,
            issue_key=run.issue_key,
            base_branch=base_branch,
            integration_branch=integration_branch,
            workspace_key=workspace_key,
            github_installation_token=installation_token,
        )
        prepared_repo = validate_execution_repo(
            checkout_root=checkout_root,
            repo_dir=run_repo_dir,
            run_id=run.run_id,
            execution_branch=execution_branch,
            workspace_key=workspace_key,
        )
    except ProjectRepoCheckoutError as exc:
        raise RetryableRepoSetupError(str(exc)) from exc

    return RepoSetupPreparation(
        prepared_repo=prepared_repo,
        actions_taken=(
            "Ensured shared repository checkout is present and current",
            "Ensured execution worktree exists on the run branch with execution metadata",
            "Validated execution repository metadata, branch, and cleanliness",
        ),
    )
