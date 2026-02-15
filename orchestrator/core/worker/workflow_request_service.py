from __future__ import annotations

from pathlib import Path

from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.worker.queue_selector import coerce_positive_int
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
    )


def _resolve_execution_repo_dir(*, settings, tenant: Tenant, project: Project | None) -> str:
    if project is None:
        return str(Path.cwd())
    repo_dir = project_repo_dir(
        base_dir=settings.project_repo_checkout_base_dir,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
    )
    if repo_dir.is_dir():
        return str(repo_dir)
    return str(Path.cwd())
