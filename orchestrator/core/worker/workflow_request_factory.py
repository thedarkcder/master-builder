from __future__ import annotations

import logging
import json

from sqlalchemy import select

from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.project_app_planner import (
    ensure_project_app,
    normalize_project_app_planner_output,
    scan_repo_for_project_apps,
)
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.runs.human_input_service import answered_human_inputs_for_attempt
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.worker.workflow_request_branching import (
    normalize_branch,
    repo_full_name_from_repository,
    resolve_branch_from_open_pull_requests,
    resolve_integration_branch,
)
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
from orchestrator.core.worker.capabilities import resolve_worker_capability_context
from orchestrator.core.worker.workspace import resolve_worker_workspace_key
from orchestrator.core.workflow.checkpoints import checkpoint_kind_for_stage
from orchestrator.core.workflow.execution_snapshot import (
    ExecutionSnapshot,
    load_parsed_trigger_context_from_plan,
)
from orchestrator.core.workflow.runner import QaCaptureTarget, WorkflowRequest
from orchestrator.storage.models import ProjectApp, Run, Tenant
from orchestrator.tools.github_app import github_client_from_tenant_config


logger = logging.getLogger(__name__)
_entry_checkpoint = entry_checkpoint
_resolve_branch_from_open_pull_requests = resolve_branch_from_open_pull_requests
_PROJECT_DEMO_TARGET_ORDER: tuple[QaCaptureTarget, ...] = ("browser", "ios", "android")
_IOS_PROJECT_MARKERS = ("ios", "iphone", "ipad", "swiftui", "xcode", "xcuitest")
_ANDROID_PROJECT_MARKERS = ("android", "espresso", "uiautomator")
_BROWSER_PROJECT_MARKERS = ("browser", "frontend", "web app", "website", "nextjs", "vite")


def _require_positive_policy_int(effective_policy: dict, field_name: str) -> int:
    raw_value = effective_policy.get(field_name)
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Effective policy field {field_name} must be a positive integer") from exc
    if parsed_value < 1:
        raise ValueError(f"Effective policy field {field_name} must be a positive integer")
    return parsed_value


def _project_app_descriptor(app: ProjectApp) -> str:
    deployment_config = getattr(app, "deployment_config", {}) or {}
    return " ".join(
        [
            str(getattr(app, "detected_runtime", "") or ""),
            str(getattr(app, "detected_language", "") or ""),
            str(getattr(app, "build_strategy", "") or ""),
            str(getattr(app, "name", "") or ""),
            str(getattr(app, "source_path", "") or ""),
            json.dumps(deployment_config, sort_keys=True, default=str),
        ]
    ).lower()


def _project_app_declares_website(app: ProjectApp) -> bool:
    deployment_config = getattr(app, "deployment_config", {}) or {}
    if not isinstance(deployment_config, dict):
        return False
    services = deployment_config.get("services")
    if not isinstance(services, list):
        return False
    return any(isinstance(service, dict) and service.get("kind") == "website" for service in services)


def _project_demo_capture_target_sources(*, session, tenant_id: str, project_id: str) -> dict[QaCaptureTarget, tuple[str, ...]]:  # noqa: ANN001
    apps = (
        session.execute(
            select(ProjectApp).where(
                ProjectApp.tenant_id == tenant_id,
                ProjectApp.project_id == project_id,
            )
        )
        .scalars()
        .all()
    )
    return _demo_capture_target_sources_from_apps(apps)


def _demo_capture_target_sources_from_apps(apps: list[object] | tuple[object, ...]) -> dict[QaCaptureTarget, tuple[str, ...]]:
    sources: dict[QaCaptureTarget, set[str]] = {target: set() for target in _PROJECT_DEMO_TARGET_ORDER}
    for app in apps:
        descriptor = _project_app_descriptor(app)
        source_path = str(getattr(app, "source_path", "") or "").strip() or "."
        if _project_app_declares_website(app) or any(marker in descriptor for marker in _BROWSER_PROJECT_MARKERS):
            sources["browser"].add(source_path)
        if any(marker in descriptor for marker in _IOS_PROJECT_MARKERS):
            sources["ios"].add(source_path)
        if any(marker in descriptor for marker in _ANDROID_PROJECT_MARKERS):
            sources["android"].add(source_path)
    return {
        target: tuple(sorted(target_sources))
        for target, target_sources in sources.items()
        if target_sources
    }


def _refresh_project_demo_capture_target_sources_from_checkout(
    *,
    session,
    tenant_id: str,
    project_id: str,
    checkout_path: str,
    run_id: str,
    scan_repo_for_project_apps_fn=None,
    normalize_project_app_planner_output_fn=None,
    ensure_project_app_fn=None,
) -> dict[QaCaptureTarget, tuple[str, ...]]:  # noqa: ANN001
    if scan_repo_for_project_apps_fn is None:
        scan_repo_for_project_apps_fn = scan_repo_for_project_apps
    if normalize_project_app_planner_output_fn is None:
        normalize_project_app_planner_output_fn = normalize_project_app_planner_output
    if ensure_project_app_fn is None:
        ensure_project_app_fn = ensure_project_app

    analysis_source = f"workflow_request:{run_id}"
    pre_scan_candidates = scan_repo_for_project_apps_fn(
        checkout_path=checkout_path,
        analysis_source=analysis_source,
    )
    normalized_candidates = normalize_project_app_planner_output_fn(
        pre_scan_candidates=pre_scan_candidates,
        runtime_payload=None,
        analysis_source=analysis_source,
    )
    refreshed_apps = [
        ensure_project_app_fn(
            session,
            tenant_id=tenant_id,
            project_id=project_id,
            candidate=candidate,
        )
        for candidate in normalized_candidates
    ]
    flush = getattr(session, "flush", None)
    if callable(flush):
        flush()
    return _demo_capture_target_sources_from_apps(tuple(refreshed_apps))


def _project_demo_capture_targets(source_map: dict[QaCaptureTarget, tuple[str, ...]]) -> tuple[QaCaptureTarget, ...]:
    return tuple(target for target in _PROJECT_DEMO_TARGET_ORDER if target in source_map)


def _resolve_base_branch(
    *,
    session,
    settings,
    tenant: Tenant,
    project,
    remediation_base_branch: str | None,
    github_client_from_tenant_config_fn,
    resolve_scoped_secret_ref_fn,
    resolve_platform_secret_ref_fn,
) -> str:  # noqa: ANN001
    if remediation_base_branch:
        return remediation_base_branch

    project_environment = getattr(project, "environment", {}) if project is not None else {}
    configured_default_branch = (
        project_environment.get("default_branch") if isinstance(project_environment, dict) else None
    )
    normalized_configured_default_branch = normalize_branch(configured_default_branch)
    if normalized_configured_default_branch:
        return normalized_configured_default_branch

    if project is None:
        raise ValueError("Run project routing is required before resolving the repository base branch")
    github_repository = str(getattr(project, "github_repository", "") or "").strip()
    repo_full_name = repo_full_name_from_repository(github_repository)
    if repo_full_name is None:
        raise ValueError("Project GitHub repository must be a GitHub owner/repo URL before resolving base branch")
    github_config_raw = getattr(tenant, "github_config", {})
    github_config = github_config_raw if isinstance(github_config_raw, dict) else {}
    if not github_config:
        raise ValueError(
            "Project environment.default_branch is not configured and tenant GitHub credentials are unavailable "
            "to resolve the repository default branch"
        )

    github_client = github_client_from_tenant_config_fn(
        github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref_fn(
            session,
            secret_ref=secret_ref,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            encryption_key=settings.secrets_encryption_key,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref_fn(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )
    resolved_default_branch = normalize_branch(
        github_client.get_repository_default_branch(
            repo_full_name=repo_full_name,
            github_repository=github_repository,
        )
    )
    if not resolved_default_branch:
        raise ValueError("GitHub repository default_branch resolved empty")
    return resolved_default_branch


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
    scan_repo_for_project_apps_fn=None,
    normalize_project_app_planner_output_fn=None,
    ensure_project_app_fn=None,
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
    max_loops = _require_positive_policy_int(effective_policy, "max_dev_test_review_loops")
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
    checkpoint_payload = None
    if checkpoint is not None:
        checkpoint_payload = ExecutionSnapshot.require(
            checkpoint.payload_json,
            allow_empty=False,
        ).dump()
    remediation_base_branch = extract_remediation_base_ref(parsed_trigger_context, normalize_branch)
    base_branch = _resolve_base_branch(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        remediation_base_branch=remediation_base_branch,
        github_client_from_tenant_config_fn=github_client_from_tenant_config_fn,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref_fn,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref_fn,
    )
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
    project_id = project.project_id if project is not None else run.project_id
    if bool(effective_policy.get("qa_demo_recording_enabled")) and execution_repo_dir:
        project_demo_capture_target_sources = _refresh_project_demo_capture_target_sources_from_checkout(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project_id,
            checkout_path=execution_repo_dir,
            run_id=run.run_id,
            scan_repo_for_project_apps_fn=scan_repo_for_project_apps_fn,
            normalize_project_app_planner_output_fn=normalize_project_app_planner_output_fn,
            ensure_project_app_fn=ensure_project_app_fn,
        )
    else:
        project_demo_capture_target_sources = _project_demo_capture_target_sources(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project_id,
        )

    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        workflow_id=run.workflow_id,
        project_id=project_id,
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
        project_demo_capture_targets=_project_demo_capture_targets(project_demo_capture_target_sources),
        project_demo_capture_target_sources=dict(project_demo_capture_target_sources),
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
