from __future__ import annotations

from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.worker.queue_selector import coerce_positive_int
from orchestrator.core.worker_capabilities import parse_worker_capabilities
from orchestrator.core.workflow.runner import WorkflowRequest
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.project_repo_checkout import project_repo_dir



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
    execution_repo_dir = _resolve_execution_repo_dir(settings=settings, tenant=tenant, project=project)
    available_worker_capabilities = sorted(
        parse_worker_capabilities(getattr(settings, "worker_capabilities", ""))
    )
    current_worker_capability = available_worker_capabilities[0] if available_worker_capabilities else "linux"
    base_branch = _normalize_branch(project.environment.get("default_branch") if project is not None else None) or "main"
    integration_branch = _normalize_branch(run.branch) or f"feature/{run.issue_key}"
    trigger_context = _extract_trigger_context(run.plan)
    pr_number = _extract_pr_number(trigger_context)

    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else run.project_id,
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
        pr_number=pr_number,
        trigger_context=trigger_context,
    )


def _resolve_execution_repo_dir(*, settings, tenant: Tenant, project: Project | None) -> str:
    if project is None:
        raise ValueError("Run project routing is required before workflow execution")
    repo_dir = project_repo_dir(
        base_dir=settings.project_repo_checkout_base_dir,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )
    if not repo_dir.is_dir():
        raise ValueError(
            "Project repository checkout is missing for workflow execution "
            f"(tenant_id={tenant.tenant_id}, project_id={project.project_id}, repo_dir={repo_dir})"
        )
    return str(repo_dir)


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
