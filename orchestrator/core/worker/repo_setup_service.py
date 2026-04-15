from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.agent_runtime_resolver import (
    resolve_execution_profile_for_selector,
)
from orchestrator.core.codex_runtime import CodexRuntimeError, build_runtime_for_execution_profile
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.runtime_stage_session import RuntimeStageSession
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.project_repo_checkout import (
    PreparedExecutionRepo,
    execution_branch_name,
    project_checkout_root_dir,
    project_repo_dir,
    project_run_repo_dir,
    validate_execution_repo,
)

_REPO_SETUP_SELECTOR = "repo_setup.prepare"


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
    snapshot = ExecutionSnapshot.require(plan, allow_empty=True)
    raw_attempts = snapshot.context.execution_context.get("repo_setup_attempts")
    try:
        return max(0, int(raw_attempts))
    except (TypeError, ValueError):
        return 0


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
    expected_run_repo_dir = project_run_repo_dir(
        base_dir=settings.project_repo_checkout_base_dir,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        run_id=run.run_id,
        workspace_key=workspace_key,
    )
    shared_repo_dir = project_repo_dir(
        base_dir=settings.project_repo_checkout_base_dir,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )
    execution_branch = execution_branch_name(issue_key=run.issue_key, run_id=run.run_id)
    runtime_profile = resolve_execution_profile_for_selector(
        session=session,
        settings=settings,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        selector=_REPO_SETUP_SELECTOR,
    )
    runtime = build_runtime_for_execution_profile(
        session=session,
        settings=settings,
        profile=runtime_profile,
    )
    context = AgentInvocationContext(
        channel="worker",
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        command="repo_setup",
        stage="prepare",
        working_dir=str(checkout_root),
        workflow_id=getattr(run, "workflow_id", None),
        issue_key=run.issue_key,
        run_id=run.run_id,
        attempt=int(getattr(run, "attempt_number", 1) or 1),
        reasoning_effort="medium",
        issue_description_chars=len(str(getattr(run, "issue_description", "") or "")),
    )
    stage_session = RuntimeStageSession.create(
        runtime=runtime,
        context=context,
        policy_stage="repo_setup",
        session=session,
        settings=settings,
        issue_key=run.issue_key,
    )
    try:
        payload = stage_session.invoke_json(
            system_prompt=render_prompt("repo_setup/prepare_system.j2"),
            user_prompt=render_prompt(
                "repo_setup/prepare_user.j2",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                project_name=project.name,
                github_repository=project.github_repository,
                run_id=run.run_id,
                issue_key=run.issue_key,
                issue_summary=run.issue_summary or "",
                issue_description=run.issue_description or "",
                checkout_root=str(checkout_root),
                shared_repo_dir=str(shared_repo_dir),
                expected_run_repo_dir=str(expected_run_repo_dir),
                workspace_key=workspace_key,
                execution_branch=execution_branch,
                base_branch=base_branch,
                integration_branch=integration_branch,
                **stage_session.tooling.governed_prompt_context(),
            ),
            require_json=False,
        )
    except CodexRuntimeError as exc:
        raise RetryableRepoSetupError(f"Repo setup runtime failed: {exc}") from exc

    if not isinstance(payload, dict):
        raise RetryableRepoSetupError("Repo setup runtime did not return a JSON object")
    tool_bridge_error = str(payload.get("_tool_bridge_error") or "").strip()
    if tool_bridge_error:
        raise RetryableRepoSetupError(f"Repo setup runtime bridge failed: {tool_bridge_error}")

    outcome = str(payload.get("outcome") or "").strip().lower()
    if outcome == "retryable_failure":
        failure_reason = str(payload.get("failure_reason") or "").strip() or "Repo setup returned retryable failure"
        raise RetryableRepoSetupError(failure_reason)
    if outcome == "terminal_failure":
        failure_reason = str(payload.get("failure_reason") or "").strip() or "Repo setup returned terminal failure"
        raise TerminalRepoSetupError(failure_reason)
    if outcome != "ready":
        raise RetryableRepoSetupError(f"Repo setup returned unsupported outcome '{outcome or '<missing>'}'")

    execution_repo_dir = str(payload.get("execution_repo_dir") or "").strip()
    if not execution_repo_dir:
        raise RetryableRepoSetupError("Repo setup did not return execution_repo_dir")
    prepared_repo = validate_execution_repo(
        checkout_root=checkout_root,
        repo_dir=Path(execution_repo_dir),
        run_id=run.run_id,
        execution_branch=str(payload.get("execution_branch") or "").strip() or execution_branch,
        workspace_key=str(payload.get("workspace_key") or "").strip() or workspace_key,
    )
    actions_taken = payload.get("actions_taken")
    if not isinstance(actions_taken, list):
        normalized_actions: tuple[str, ...] = ()
    else:
        normalized_actions = tuple(
            text for text in (str(item or "").strip() for item in actions_taken) if text
        )
    return RepoSetupPreparation(
        prepared_repo=prepared_repo,
        actions_taken=normalized_actions,
    )
