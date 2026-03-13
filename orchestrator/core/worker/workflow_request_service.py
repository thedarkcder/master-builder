from __future__ import annotations

from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.worker.queue_selector import coerce_positive_int
from orchestrator.core.worker_capabilities import parse_worker_capabilities
from orchestrator.core.workflow.runner import WorkflowRequest
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.project_repo_checkout import (
    ProjectRepoCheckoutError,
    ensure_run_worktree,
    read_run_worktree_metadata,
    validate_run_worktree,
)



def build_workflow_request_for_run(
    *,
    tenant: Tenant,
    run: Run,
    project: Project | None,
    effective_policy: dict,
    settings,
) -> WorkflowRequest:  # noqa: ANN001
    max_loops = coerce_positive_int(
        effective_policy.get("max_dev_test_review_loops"),
        default=1,
    )
    suggested_test_commands_raw = effective_policy.get("allowed_commands") or []
    suggested_test_commands: list[str] = []
    for command in suggested_test_commands_raw:
        command_text = str(command).strip()
        enforce_safe_command(command_text)
        suggested_test_commands.append(command_text)

    issue_description = run.issue_description or ""
    if project is not None:
        project_context = (
            "\n\nProject routing context:\n"
            f"- project_id: {project.project_id}\n"
            f"- project_name: {project.name}\n"
            f"- github_repository: {project.github_repository}\n"
            f"- jira_project_key: {project.jira_project_key}\n"
        )
        issue_description = f"{issue_description}{project_context}".strip()
    else:
        issue_description = issue_description.strip()
    project_environment = getattr(project, "environment", {}) if project is not None else {}
    default_branch = project_environment.get("default_branch") if isinstance(project_environment, dict) else None
    base_branch = _normalize_branch(default_branch) or "main"
    integration_branch = _normalize_branch(getattr(run, "branch", None)) or f"feature/{run.issue_key}"
    execution_repo_dir, execution_branch, start_point_ref, start_point_sha = _resolve_execution_repo_dir(
        settings=settings,
        tenant=tenant,
        run=run,
        project=project,
        base_branch=base_branch,
        integration_branch=integration_branch,
    )
    available_worker_capabilities = sorted(
        parse_worker_capabilities(getattr(settings, "worker_capabilities", ""))
    )
    current_worker_capability = available_worker_capabilities[0] if available_worker_capabilities else "linux"
    trigger_context = _extract_trigger_context(getattr(run, "plan", None))
    pr_number = _extract_pr_number(trigger_context)

    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else run.project_id,
        project_name=project.name if project is not None else None,
        github_repository=project.github_repository if project is not None else None,
        jira_project_key=project.jira_project_key if project is not None else None,
        run_id=run.run_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary or f"Execute {run.issue_key}",
        issue_description=issue_description,
        max_dev_test_review_loops=max_loops,
        suggested_test_commands=suggested_test_commands,
        execution_repo_dir=execution_repo_dir,
        current_worker_capability=current_worker_capability,
        available_worker_capabilities=available_worker_capabilities,
        base_branch=base_branch,
        integration_branch=integration_branch,
        pr_target_branch=base_branch,
        execution_branch=execution_branch,
        start_point_ref=start_point_ref,
        start_point_sha=start_point_sha,
        pr_number=pr_number,
        trigger_context=trigger_context,
    )


def _resolve_execution_repo_dir(
    *,
    settings,
    tenant: Tenant,
    run: Run,
    project: Project | None,
    base_branch: str,
    integration_branch: str,
) -> tuple[str, str, str | None, str | None]:
    if project is None:
        raise ValueError("Run project routing is required before workflow execution")
    try:
        repo_dir, execution_branch = ensure_run_worktree(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project=project,
            run_id=run.run_id,
            issue_key=run.issue_key,
            base_branch=base_branch,
            integration_branch=integration_branch,
        )
    except ProjectRepoCheckoutError as exc:
        raise ValueError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "Run worktree bootstrap failed "
            f"(tenant_id={tenant.tenant_id}, project_id={project.project_id}, run_id={run.run_id}): {exc}"
        ) from exc
    validation_error = validate_run_worktree(
        repo_dir=repo_dir,
        run_id=run.run_id,
        execution_branch=execution_branch,
    )
    if validation_error is not None:
        raise ValueError(
            "Run worktree is invalid for workflow execution "
            f"(tenant_id={tenant.tenant_id}, project_id={project.project_id}, run_id={run.run_id}, "
            f"repo_dir={repo_dir}, reason={validation_error})"
        )
    metadata = read_run_worktree_metadata(repo_dir=repo_dir) or {}
    start_point_ref = str(metadata.get("start_point_ref") or "").strip() or None
    start_point_sha = str(metadata.get("start_point_sha") or "").strip() or None
    return str(repo_dir), execution_branch, start_point_ref, start_point_sha


def _normalize_branch(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _extract_trigger_context(plan: object) -> dict | None:
    if not isinstance(plan, dict):
        return None
    trigger_context = plan.get("trigger_context")
    if isinstance(trigger_context, dict):
        return trigger_context
    return None


def _extract_pr_number(trigger_context: dict | None) -> int | None:
    if not isinstance(trigger_context, dict):
        return None
    value = trigger_context.get("pr_number")
    if isinstance(value, int) and value > 0:
        return value
    return None
