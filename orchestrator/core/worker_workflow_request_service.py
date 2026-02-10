from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from orchestrator.core.enforcement_context import build_agent_enforcement_context
from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.worker_queue_selector import coerce_positive_int
from orchestrator.core.workflow_runner import WorkflowRequest
from orchestrator.storage.models import Project, Run, Tenant


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
    max_runtime_minutes = coerce_positive_int(
        effective_policy.get("max_runtime_minutes"),
        default=30,
    )
    suggested_test_commands_raw = effective_policy.get("allowed_commands") or []
    suggested_test_commands: list[str] = []
    for command in suggested_test_commands_raw:
        command_text = str(command).strip()
        enforce_safe_command(command_text)
        suggested_test_commands.append(command_text)

    enforcement_context = _cached_enforcement_context(settings.required_codex_assets_version or "")

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
    issue_description_with_enforcement = (
        f"{issue_description}\n\n{enforcement_context}" if issue_description else enforcement_context
    )

    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary or f"Execute {run.issue_key}",
        issue_description=issue_description_with_enforcement,
        max_dev_test_review_loops=max_loops,
        max_runtime_minutes=max_runtime_minutes,
        suggested_test_commands=suggested_test_commands,
    )


@lru_cache(maxsize=1)
def _cached_enforcement_context(required_assets_version: str) -> str:
    repo_root = Path(__file__).resolve().parents[2]
    return build_agent_enforcement_context(
        repo_root=repo_root,
        required_assets_version=required_assets_version or None,
    )
