from __future__ import annotations

import logging

from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.run_human_input_service import answered_human_inputs_for_attempt
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.worker.workflow_request_branching import (
    normalize_branch,
    resolve_branch_from_open_pull_requests,
    resolve_integration_branch,
)
from orchestrator.core.worker.queue_selector import coerce_positive_int
from orchestrator.core.worker.repo_setup_service import (
    RetryableRepoSetupError,
    TerminalRepoSetupError,
    prepare_execution_repo_for_run,
)
from orchestrator.core.worker.workflow_request_resume import (
    entry_checkpoint,
    extract_pr_number,
    extract_remediation_base_ref,
    extract_remediation_head_ref,
    extract_trigger_context,
)
from orchestrator.core.worker_capabilities import resolve_worker_capability_context
from orchestrator.core.worker_workspace import resolve_worker_workspace_key
from orchestrator.core.workflow.checkpoints import checkpoint_kind_for_stage
from orchestrator.core.workflow.execution_snapshot import (
    ExecutionSnapshot,
    load_parsed_trigger_context_from_plan,
)
from orchestrator.core.workflow.runner import WorkflowRequest
from orchestrator.storage.models import Run, Tenant
from orchestrator.tools.github_app import github_client_from_tenant_config


logger = logging.getLogger(__name__)
_entry_checkpoint = entry_checkpoint
_resolve_branch_from_open_pull_requests = resolve_branch_from_open_pull_requests


def build_workflow_request(
    *,
    session,
    tenant: Tenant,
    run: Run,
    project,
    effective_policy: dict,
    settings,
    prepare_execution_repo_for_run_fn=None,
    github_client_from_tenant_config_fn=None,
    resolve_scoped_secret_ref_fn=None,
    resolve_platform_secret_ref_fn=None,
    answered_human_inputs_for_attempt_fn=None,
    entry_checkpoint_fn=None,
    resolve_branch_from_open_pull_requests_fn=None,
) -> WorkflowRequest:  # noqa: ANN001
    if prepare_execution_repo_for_run_fn is None:
        prepare_execution_repo_for_run_fn = prepare_execution_repo_for_run
    if github_client_from_tenant_config_fn is None:
        github_client_from_tenant_config_fn = github_client_from_tenant_config
    if resolve_scoped_secret_ref_fn is None:
        resolve_scoped_secret_ref_fn = resolve_scoped_secret_ref
    if resolve_platform_secret_ref_fn is None:
        resolve_platform_secret_ref_fn = resolve_platform_secret_ref
    if answered_human_inputs_for_attempt_fn is None:
        answered_human_inputs_for_attempt_fn = answered_human_inputs_for_attempt
    if entry_checkpoint_fn is None:
        entry_checkpoint_fn = entry_checkpoint
    if resolve_branch_from_open_pull_requests_fn is None:
        resolve_branch_from_open_pull_requests_fn = resolve_branch_from_open_pull_requests
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

    issue_description = str(getattr(run, "issue_description", "") or "")
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

    trigger_context = extract_trigger_context(getattr(run, "plan", None))
    parsed_trigger_context = load_parsed_trigger_context_from_plan(getattr(run, "plan", None))
    entry_mode = str(getattr(run, "entry_mode", "fresh") or "fresh").strip().lower() or "fresh"
    entry_stage = str(getattr(run, "entry_stage", "") or "").strip().lower() or None
    checkpoint = entry_checkpoint_fn(session=session, run=run)
    checkpoint_payload = dict(checkpoint.payload_json) if checkpoint is not None else None
    if checkpoint_payload is not None and ExecutionSnapshot.load(checkpoint_payload) is None:
        logger.warning(
            "Resume checkpoint payload is not a supported execution snapshot shape; "
            "executor will handle resume failure safely "
            "(workflow_id=%s, run_id=%s, checkpoint_id=%s)",
            run.workflow_id,
            run.run_id,
            checkpoint.checkpoint_id if checkpoint is not None else None,
        )
        checkpoint_payload = None
    project_environment = getattr(project, "environment", {}) if project is not None else {}
    default_branch = project_environment.get("default_branch") if isinstance(project_environment, dict) else None
    remediation_base_branch = extract_remediation_base_ref(parsed_trigger_context, normalize_branch)
    base_branch = remediation_base_branch or normalize_branch(default_branch) or "main"
    remediation_head_branch = extract_remediation_head_ref(parsed_trigger_context, normalize_branch)
    integration_branch = resolve_integration_branch(
        session=session,
        settings=settings,
        tenant=tenant,
        run=run,
        project=project,
        base_branch=base_branch,
        remediation_head_branch=remediation_head_branch,
        github_client_from_tenant_config_fn=github_client_from_tenant_config_fn,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref_fn,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref_fn,
        resolve_branch_from_open_pull_requests_fn=resolve_branch_from_open_pull_requests_fn,
    )
    execution_repo_dir, execution_branch, start_point_ref, start_point_sha, workspace_key = _resolve_execution_repo_dir(
        session=session,
        settings=settings,
        tenant=tenant,
        run=run,
        project=project,
        base_branch=base_branch,
        integration_branch=integration_branch,
        prepare_execution_repo_for_run_fn=prepare_execution_repo_for_run_fn,
    )
    capability_context = resolve_worker_capability_context(
        raw_value=getattr(settings, "worker_capabilities", ""),
        source="ORCHESTRATOR_WORKER_CAPABILITIES",
    )
    pr_number = extract_pr_number(
        parsed_trigger_context=parsed_trigger_context,
        trigger_context=trigger_context,
    )
    human_inputs = answered_human_inputs_for_attempt_fn(
        session=session,
        settings=settings,
        workflow_id=run.workflow_id,
        consumed_by_run_id=run.run_id,
    )

    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        workflow_id=run.workflow_id,
        project_id=project.project_id if project is not None else run.project_id,
        project_name=project.name if project is not None else None,
        github_repository=project.github_repository if project is not None else None,
        jira_project_key=project.jira_project_key if project is not None else None,
        run_id=run.run_id,
        attempt_number=int(getattr(run, "attempt_number", 1) or 1),
        issue_key=run.issue_key,
        issue_summary=run.issue_summary or f"Execute {run.issue_key}",
        issue_description=issue_description,
        max_dev_test_review_loops=max_loops,
        allow_pr_creation=bool(effective_policy.get("allow_pr_creation", False)),
        suggested_test_commands=suggested_test_commands,
        execution_repo_dir=execution_repo_dir,
        workspace_key=workspace_key,
        current_worker_capability=capability_context.current,
        available_worker_capabilities=capability_context.available,
        base_branch=base_branch,
        integration_branch=integration_branch,
        pr_target_branch=base_branch,
        execution_branch=execution_branch,
        start_point_ref=start_point_ref,
        start_point_sha=start_point_sha,
        pr_number=pr_number,
        trigger_context=trigger_context,
        entry_mode=entry_mode,
        entry_stage=entry_stage,
        checkpoint_kind=(
            checkpoint.checkpoint_kind
            if checkpoint is not None
            else (checkpoint_kind_for_stage(entry_stage) if entry_mode == "resume" and entry_stage else None)
        ),
        checkpoint_id=checkpoint.checkpoint_id if checkpoint is not None else None,
        checkpoint_payload=checkpoint_payload,
        checkpoint_session_id=checkpoint.codex_session_id if checkpoint is not None else None,
        human_inputs=human_inputs,
    )


def _resolve_execution_repo_dir(
    *,
    session,
    settings,
    tenant: Tenant,
    run: Run,
    project,
    base_branch: str,
    integration_branch: str,
    prepare_execution_repo_for_run_fn,
) -> tuple[str, str, str | None, str | None, str]:
    if project is None:
        raise ValueError("Run project routing is required before workflow execution")
    workspace_key = resolve_worker_workspace_key(settings=settings)
    try:
        preparation = prepare_execution_repo_for_run_fn(
            session=session,
            settings=settings,
            tenant=tenant,
            run=run,
            project=project,
            base_branch=base_branch,
            integration_branch=integration_branch,
            workspace_key=workspace_key,
        )
    except (RetryableRepoSetupError, TerminalRepoSetupError):
        raise
    except Exception as exc:  # noqa: BLE001
        raise ValueError(
            "Run repo setup failed "
            f"(tenant_id={tenant.tenant_id}, project_id={project.project_id}, run_id={run.run_id}): {exc}"
        ) from exc
    prepared_repo = preparation.prepared_repo
    return (
        str(prepared_repo.repo_dir),
        prepared_repo.execution_branch,
        prepared_repo.start_point_ref,
        prepared_repo.start_point_sha,
        prepared_repo.workspace_key,
    )
