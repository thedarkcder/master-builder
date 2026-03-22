from __future__ import annotations

import logging

from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.run_human_input_service import answered_human_inputs_for_request
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.worker_workspace import resolve_worker_workspace_key
from orchestrator.core.worker.queue_selector import coerce_positive_int
from orchestrator.core.worker_capabilities import parse_worker_capabilities
from orchestrator.core.workflow.runner import WorkflowRequest
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.project_repo_checkout import (
    ProjectRepoCheckoutError,
    ensure_run_worktree,
    read_run_worktree_metadata,
    validate_run_worktree,
)
from orchestrator.tools.repo_allowlist import normalize_repo_identifier


logger = logging.getLogger(__name__)


def build_workflow_request_for_run(
    *,
    session,
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
    trigger_context = _extract_trigger_context(getattr(run, "plan", None))
    project_environment = getattr(project, "environment", {}) if project is not None else {}
    default_branch = project_environment.get("default_branch") if isinstance(project_environment, dict) else None
    remediation_base_branch = _extract_remediation_base_ref(trigger_context)
    base_branch = remediation_base_branch or _normalize_branch(default_branch) or "main"
    remediation_head_branch = _extract_remediation_head_ref(trigger_context)
    integration_branch = _resolve_integration_branch(
        session=session,
        settings=settings,
        tenant=tenant,
        run=run,
        project=project,
        base_branch=base_branch,
        remediation_head_branch=remediation_head_branch,
    )
    execution_repo_dir, execution_branch, start_point_ref, start_point_sha, workspace_key = _resolve_execution_repo_dir(
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
    pr_number = _extract_pr_number(trigger_context)
    resume_mode = _extract_resume_mode(trigger_context)
    resume_stage = _extract_resume_stage(trigger_context)
    resume_session_id = _extract_resume_session_id(trigger_context)
    resume_source_plan = _extract_resume_source_plan(trigger_context)
    human_input_request_ids = _extract_human_input_request_ids(trigger_context)
    human_inputs = answered_human_inputs_for_request(
        session=session,
        settings=settings,
        request_ids=human_input_request_ids,
    ) if human_input_request_ids else []

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
        allow_pr_creation=bool(effective_policy.get("allow_pr_creation", False)),
        suggested_test_commands=suggested_test_commands,
        execution_repo_dir=execution_repo_dir,
        workspace_key=workspace_key,
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
        resume_mode=resume_mode,
        resume_stage=resume_stage,
        resume_session_id=resume_session_id,
        resume_source_plan=resume_source_plan,
        human_inputs=human_inputs,
    )


def _resolve_execution_repo_dir(
    *,
    settings,
    tenant: Tenant,
    run: Run,
    project: Project | None,
    base_branch: str,
    integration_branch: str,
) -> tuple[str, str, str | None, str | None, str]:
    if project is None:
        raise ValueError("Run project routing is required before workflow execution")
    workspace_key = resolve_worker_workspace_key(settings=settings)
    try:
        repo_dir, execution_branch = ensure_run_worktree(
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=tenant.tenant_id,
            project=project,
            run_id=run.run_id,
            issue_key=run.issue_key,
            base_branch=base_branch,
            integration_branch=integration_branch,
            workspace_key=workspace_key,
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
        workspace_key=workspace_key,
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
    return str(repo_dir), execution_branch, start_point_ref, start_point_sha, workspace_key


def _normalize_branch(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _resolve_integration_branch(
    *,
    session,
    settings,
    tenant: Tenant,
    run: Run,
    project: Project | None,
    base_branch: str,
    remediation_head_branch: str | None = None,
) -> str:
    if remediation_head_branch:
        run.branch = remediation_head_branch
        return remediation_head_branch

    run_branch = _normalize_branch(getattr(run, "branch", None))
    if run_branch:
        return run_branch

    reused_branch = _resolve_branch_from_open_pull_requests(
        session=session,
        settings=settings,
        tenant=tenant,
        run=run,
        project=project,
        base_branch=base_branch,
    )
    integration_branch = reused_branch or f"feature/{run.issue_key}"
    run.branch = integration_branch
    return integration_branch


def _resolve_branch_from_open_pull_requests(
    *,
    session,
    settings,
    tenant: Tenant,
    run: Run,
    project: Project | None,
    base_branch: str,
) -> str | None:
    if project is None:
        return None
    github_repository = str(project.github_repository or "").strip()
    if not github_repository:
        return None
    github_config_raw = getattr(tenant, "github_config", {})
    github_config = github_config_raw if isinstance(github_config_raw, dict) else {}
    if not github_config:
        return None

    issue_key = str(run.issue_key or "").strip()
    if not issue_key:
        return None
    issue_key_lower = issue_key.lower()
    repo_full_name = _repo_full_name(github_repository)
    if repo_full_name is None:
        return None

    try:
        github_client = github_client_from_tenant_config(
            github_config,
            tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
                session,
                secret_ref=secret_ref,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                encryption_key=settings.secrets_encryption_key,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
        pull_requests = github_client.list_open_pull_requests(repo_full_name=repo_full_name, limit=100)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workflow_branch_lookup_failed tenant_id=%s project_id=%s run_id=%s issue_key=%s error=%s",
            tenant.tenant_id,
            project.project_id,
            run.run_id,
            run.issue_key,
            exc,
        )
        return None

    for pull_request in pull_requests:
        if str(pull_request.base_ref or "").strip() != base_branch:
            continue
        title_lower = str(pull_request.title or "").lower()
        head_ref = _normalize_branch(pull_request.head_ref)
        if not head_ref:
            continue
        head_lower = head_ref.lower()
        if issue_key_lower not in head_lower and issue_key_lower not in title_lower:
            continue
        return head_ref
    return None


def _repo_full_name(repository_url: str) -> str | None:
    normalized = normalize_repo_identifier(repository_url)
    prefix = "github.com/"
    if not normalized.startswith(prefix):
        return None
    full_name = normalized[len(prefix) :].strip("/")
    if full_name.count("/") != 1:
        return None
    return full_name


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


def _extract_remediation_head_ref(trigger_context: dict | None) -> str | None:
    if not isinstance(trigger_context, dict):
        return None
    source = str(trigger_context.get("source") or "").strip().lower()
    if source != "github_pr_review_feedback":
        return None
    return _normalize_branch(trigger_context.get("head_ref"))


def _extract_remediation_base_ref(trigger_context: dict | None) -> str | None:
    if not isinstance(trigger_context, dict):
        return None
    source = str(trigger_context.get("source") or "").strip().lower()
    if source != "github_pr_review_feedback":
        return None
    return _normalize_branch(trigger_context.get("base_ref"))


def _extract_resume_mode(trigger_context: dict | None) -> str | None:
    if not isinstance(trigger_context, dict):
        return None
    value = str(trigger_context.get("rerun_mode") or "").strip().lower()
    return value if value in {"fresh", "resume"} else None


def _extract_resume_stage(trigger_context: dict | None) -> str | None:
    if not isinstance(trigger_context, dict):
        return None
    value = str(trigger_context.get("resume_stage") or "").strip().lower()
    return value if value in {"orchestrated", "pm", "dev"} else None


def _extract_resume_session_id(trigger_context: dict | None) -> str | None:
    if not isinstance(trigger_context, dict):
        return None
    value = str(trigger_context.get("resume_session_id") or "").strip()
    return value or None


def _extract_resume_source_plan(trigger_context: dict | None) -> dict | None:
    if not isinstance(trigger_context, dict):
        return None
    value = trigger_context.get("resume_source_plan")
    if not isinstance(value, dict):
        return None
    return value


def _extract_human_input_request_ids(trigger_context: dict | None) -> list[str]:
    if not isinstance(trigger_context, dict):
        return []
    value = trigger_context.get("human_input_request_ids")
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]
