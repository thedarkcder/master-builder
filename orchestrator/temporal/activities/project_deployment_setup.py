from __future__ import annotations

from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select

from orchestrator.api.admin.github_helpers import get_project_github_branch_head_sha
from orchestrator.api.admin.deployment_release_service import (
    create_project_deployment_release,
)
from orchestrator.api.deployment_schemas import (
    ProjectDeploymentConfigWrite,
    ProjectDeploymentPolicyRead,
)
from orchestrator.api.schemas import ProjectDeploymentReleaseCreate
from orchestrator.core.config import get_settings
from orchestrator.core.deployment_setup.artifacts import (
    checkout_branch_commit as _checkout_branch_commit,
    ensure_deployment_compose_artifact as _ensure_deployment_compose_artifact,
)
from orchestrator.core.deployment_setup.workflow import (
    DEPLOYMENT_SETUP_STEP_ANALYZE,
    DEPLOYMENT_SETUP_STEP_PREPARE,
    DEPLOYMENT_SETUP_STEP_RELEASE,
)
from orchestrator.core.deployment_setup.compose_normalizer import (
    normalize_compose_for_coolify,
)
from orchestrator.core.deployment_setup.planner import run_project_deployment_planning
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.project_app_planner import (
    ensure_project_app,
)
from orchestrator.core.workflow.execution_projection import WorkflowExecutionProjection
from orchestrator.core.workflow.execution_status import mark_workflow_failed
from orchestrator.core.workflow.operation_service import (
    OPERATION_STATUS_RUNNING,
    fail_workflow_operation,
    touch_workflow_operation_attempt_heartbeat,
)
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import (
    Project,
    ProjectApp,
    Tenant,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from orchestrator.temporal.payloads import (
    ProjectDeploymentSetupActivityResult,
    ProjectDeploymentSetupWorkflowInput,
)
from orchestrator.tools.project_repo_checkout import (
    ProjectRepoCheckoutError,
    project_repo_dir,
)

try:  # pragma: no cover - exercised when temporal backend is enabled
    from temporalio import activity
except (
    ImportError
) as exc:  # pragma: no cover - exercised when temporal backend is enabled
    raise RuntimeError("Temporal backend requires temporalio to be installed") from exc


def _app_deployment_config_for_setup(
    *,
    app: ProjectApp,
    policy: ProjectDeploymentPolicyRead,
) -> dict[str, object]:
    existing = dict(app.deployment_config or {})
    source_strategy = existing.get("source_strategy") or app.build_strategy
    if source_strategy == "nixpacks":
        raise RuntimeError(
            f"Deployment setup cannot release app {app.slug} without generated deployment files"
        )
    resources = list(
        existing.get("resources") if isinstance(existing.get("resources"), list) else []
    )
    resources_by_key = {
        str(item.get("key") or "").strip(): dict(item)
        for item in resources
        if isinstance(item, dict)
    }
    for resource in policy.resources:
        resources_by_key[resource.key] = resource.model_dump(exclude_none=True)
    generated_compose_raw = (
        str(existing.get("generated_compose_raw") or "").strip() or None
    )
    normalized = ProjectDeploymentConfigWrite.model_validate(
        {
            **existing,
            "enabled": True,
            "environment_name": existing.get("environment_name") or "production",
            "source_strategy": source_strategy,
            "resources": [
                item for key, item in sorted(resources_by_key.items()) if key
            ],
        }
    )
    payload = normalized.model_dump(exclude_none=True)
    if generated_compose_raw is not None:
        payload["generated_compose_raw"] = generated_compose_raw
    if isinstance(existing.get("deployment_plan"), dict):
        payload["deployment_plan"] = dict(existing["deployment_plan"])
    return payload


def _deployment_setup_lifecycle(
    *, session, workflow_id: str
) -> WorkflowExecutionProjection:  # noqa: ANN001
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise RuntimeError(
            f"Deployment setup workflow execution is missing for {workflow_id}"
        )
    workflow_type = get_workflow_type(
        session, workflow_type_key="project_deployment_setup"
    )
    return WorkflowExecutionProjection(
        session=session, workflow=workflow, workflow_type=workflow_type
    )


def _create_initial_setup_release(
    *,
    session,
    tenant: Tenant,
    project: Project,
    deployment_app: ProjectApp,
    deployment_branch: str,
    deployment_commit_sha: str,
    workflow_id: str,
):
    return create_project_deployment_release(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        payload=ProjectDeploymentReleaseCreate(
            app_id=deployment_app.app_id,
            git_ref=deployment_branch,
            commit_sha=deployment_commit_sha,
            reason=f"Deployment setup {workflow_id}",
        ),
        requested_by_user_id=None,
    )


def _supersede_failed_deployment_setup_executions(
    *,
    session,
    tenant_id: str,
    project_id: str,
    replacement_workflow_id: str,
) -> int:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    previous_executions = (
        session.execute(
            select(WorkflowExecution).where(
                WorkflowExecution.tenant_id == tenant_id,
                WorkflowExecution.project_id == project_id,
                WorkflowExecution.workflow_type_key == "project_deployment_setup",
                WorkflowExecution.workflow_id != replacement_workflow_id,
                WorkflowExecution.status == "failed",
            )
        )
        .scalars()
        .all()
    )
    for workflow in previous_executions:
        workflow.status = "superseded"
        workflow.last_error = (
            f"Superseded by deployment setup workflow {replacement_workflow_id}"
        )
        workflow.source_workflow_id = replacement_workflow_id
        workflow.finished_at = workflow.finished_at or now
        workflow.updated_at = now
    return len(previous_executions)


def _touch_deployment_setup_attempt_heartbeat(
    *,
    workflow_id: str,
    operation_id: str,
    attempt_id: str,
) -> None:
    session_factory = create_session_factory()
    with session_factory() as session:
        attempt = (
            session.execute(
                select(WorkflowOperationAttempt)
                .join(
                    WorkflowOperation,
                    WorkflowOperation.operation_id
                    == WorkflowOperationAttempt.operation_id,
                )
                .where(
                    WorkflowOperation.workflow_id == workflow_id,
                    WorkflowOperation.operation_id == operation_id,
                    WorkflowOperationAttempt.attempt_id == attempt_id,
                    WorkflowOperationAttempt.status == OPERATION_STATUS_RUNNING,
                )
            )
            .scalars()
            .one_or_none()
        )
        if attempt is None:
            raise RuntimeError(
                "Deployment setup runtime heartbeat failed because the active workflow operation attempt was not found"
            )
        touch_workflow_operation_attempt_heartbeat(
            session,
            attempt=attempt,
            lease_owner="project_deployment_setup_activity",
        )
        session.commit()


@activity.defn(name="run_project_deployment_setup_activity")
def run_project_deployment_setup_activity(
    payload: ProjectDeploymentSetupWorkflowInput,
) -> ProjectDeploymentSetupActivityResult:
    settings = get_settings()
    session_factory = create_session_factory()
    workflow_id = str(payload.workflow_id or "").strip()
    tenant_id = str(payload.tenant_id or "").strip()
    project_id = str(payload.project_id or "").strip()
    try:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None:
                raise RuntimeError(f"Deployment setup is missing tenant {tenant_id}")
            project = session.get(Project, project_id)
            if project is None or project.tenant_id != tenant_id:
                raise RuntimeError(f"Deployment setup is missing project {project_id}")
            policy = ProjectDeploymentPolicyRead.model_validate(
                dict(project.deployment_config or {})
            )
            if not policy.enabled:
                raise RuntimeError(
                    "Deployment setup cannot start an initial release while deployments are disabled"
                )
            branch = str(policy.production_branch or "").strip()
            if not branch:
                raise RuntimeError("Deployment setup requires a production branch")
            lifecycle = _deployment_setup_lifecycle(
                session=session, workflow_id=workflow_id
            )
            analyze_operation, analyze_attempt = lifecycle.start_operation_attempt(
                operation_type=DEPLOYMENT_SETUP_STEP_ANALYZE,
                target_system="github",
                target_ref=branch,
                summary=f"Analyze deployment configuration for {branch}.",
            )

            from orchestrator.api.admin.integration_dependencies import (
                github_client_from_tenant_config,
                with_managed_github_refs,
            )
            from orchestrator.api.admin.route_helpers import (
                ensure_project_repository_checkout,
            )

            commit_sha = get_project_github_branch_head_sha(
                tenant=tenant,
                project=project,
                tenant_id=tenant_id,
                branch=branch,
                session=session,
                settings=settings,
                with_managed_github_refs_fn=with_managed_github_refs,
                resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
                resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
                github_client_from_tenant_config_fn=github_client_from_tenant_config,
            )
            try:
                ensure_project_repository_checkout(
                    session=session, tenant=tenant, project=project
                )
            except ProjectRepoCheckoutError as exc:
                raise RuntimeError(
                    f"Unable to ensure project repository checkout: {exc}"
                ) from exc

            checkout_path = project_repo_dir(
                base_dir=str(
                    getattr(settings, "project_repo_checkout_base_dir", "") or ""
                ),
                tenant_id=tenant_id,
                project_id=project_id,
            )
            _checkout_branch_commit(
                repo_dir=checkout_path, branch=branch, commit_sha=commit_sha
            )

            result = run_project_deployment_planning(
                tenant=tenant,
                project=project,
                checkout_path=str(checkout_path),
                branch=branch,
                commit_sha=commit_sha,
                analysis_source="deployment_setup",
                session=session,
                settings=settings,
                workflow_id=workflow_id,
                operation_id=analyze_operation.operation_id,
                attempt_id=analyze_attempt.attempt_id,
                attempt_number=analyze_attempt.attempt_number,
                extra_on_log_line=lambda _stream, _line: (
                    _touch_deployment_setup_attempt_heartbeat(
                        workflow_id=workflow_id,
                        operation_id=analyze_operation.operation_id,
                        attempt_id=analyze_attempt.attempt_id,
                    )
                ),
            )
            github_client = github_client_from_tenant_config(
                tenant.github_config,
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
            normalized_compose_raw = normalize_compose_for_coolify(
                result.plan.compose_raw
            ).compose_raw
            deployment_branch, deployment_commit_sha, deployment_compose_path = (
                _ensure_deployment_compose_artifact(
                    repo_dir=checkout_path,
                    project_id=project_id,
                    source_branch=branch,
                    source_commit_sha=commit_sha,
                    compose_raw=normalized_compose_raw,
                    npm_service_source_paths=tuple(
                        service.source_path
                        for service in result.plan.services
                        if service.build_strategy == "npm"
                    ),
                    token=github_client.get_installation_token(),
                )
            )
            obsolete_setup_apps = (
                session.execute(
                    select(ProjectApp).where(
                        ProjectApp.tenant_id == tenant_id,
                        ProjectApp.project_id == project_id,
                        ProjectApp.analysis_source == "deployment_setup",
                        ProjectApp.source_path != ".",
                    )
                )
                .scalars()
                .all()
            )
            for obsolete_app in obsolete_setup_apps:
                session.delete(obsolete_app)
            ensured_app = ensure_project_app(
                session,
                tenant_id=tenant_id,
                project_id=project_id,
                candidate=result.app,
            )
            ensured_config = dict(ensured_app.deployment_config or {})
            ensured_config["source_branch"] = branch
            ensured_config["source_commit_sha"] = commit_sha
            ensured_config["deployment_branch"] = deployment_branch
            ensured_config["deployment_commit_sha"] = deployment_commit_sha
            ensured_config["deployment_compose_path"] = deployment_compose_path
            ensured_config["generated_compose_raw"] = normalized_compose_raw
            ensured_app.deployment_config = ensured_config
            session.flush()
            lifecycle.complete_started_operation(
                operation=analyze_operation,
                attempt=analyze_attempt,
                summary=f"Planned {len(result.plan.services)} deployable service(s) from {branch} and published deployment compose to {deployment_branch}.",
            )

            prepare_operation, prepare_attempt = lifecycle.start_operation_attempt(
                operation_type=DEPLOYMENT_SETUP_STEP_PREPARE,
                target_system="deployment",
                target_ref=branch,
                summary=f"Prepare deployment configuration for {branch}.",
            )
            apps = (
                session.execute(
                    select(ProjectApp)
                    .where(
                        ProjectApp.tenant_id == tenant_id,
                        ProjectApp.project_id == project_id,
                        ProjectApp.source_path == ".",
                    )
                    .order_by(ProjectApp.created_at.asc(), ProjectApp.app_id.asc())
                )
                .scalars()
                .all()
            )
            if not apps:
                raise RuntimeError(
                    "Deployment setup did not persist the project deployment"
                )
            for app in apps:
                app.deployment_config = _app_deployment_config_for_setup(
                    app=app, policy=policy
                )
                if str(app.status or "").strip().lower() not in {"deploying", "live"}:
                    app.status = "ready"
                app.updated_at = datetime.now(timezone.utc)
            project.deployment_config = policy.model_dump(exclude_none=True)
            project.updated_at = datetime.now(timezone.utc)
            lifecycle.complete_started_operation(
                operation=prepare_operation,
                attempt=prepare_attempt,
                summary=f"Prepared deployment configuration for {len(result.plan.services)} service(s).",
            )

        with session_factory() as release_session:
            tenant = release_session.get(Tenant, tenant_id)
            project = release_session.get(Project, project_id)
            if tenant is None or project is None:
                raise RuntimeError(
                    "Deployment setup lost tenant/project before release creation"
                )
            lifecycle = _deployment_setup_lifecycle(
                session=release_session, workflow_id=workflow_id
            )
            deployment_app = (
                release_session.execute(
                    select(ProjectApp)
                    .where(
                        ProjectApp.tenant_id == tenant_id,
                        ProjectApp.project_id == project_id,
                        ProjectApp.source_path == ".",
                    )
                    .order_by(ProjectApp.created_at.asc(), ProjectApp.app_id.asc())
                )
                .scalars()
                .first()
            )
            if deployment_app is None:
                raise RuntimeError(
                    "Deployment setup lost project deployment app before release creation"
                )
            deployment_config = dict(deployment_app.deployment_config or {})
            release_branch = str(
                deployment_config.get("deployment_branch") or ""
            ).strip()
            release_commit_sha = str(
                deployment_config.get("deployment_commit_sha") or ""
            ).strip()
            if not release_branch or not release_commit_sha:
                raise RuntimeError(
                    "Deployment setup did not publish a deployment compose branch before release creation"
                )
            release_operation, release_attempt = lifecycle.start_operation_attempt(
                operation_type=DEPLOYMENT_SETUP_STEP_RELEASE,
                target_system="deployment",
                target_ref=release_branch,
                summary=f"Create initial release from {release_branch} at {release_commit_sha}.",
            )
            release = _create_initial_setup_release(
                session=release_session,
                tenant=tenant,
                project=project,
                deployment_app=deployment_app,
                deployment_branch=release_branch,
                deployment_commit_sha=release_commit_sha,
                workflow_id=workflow_id,
            )
            lifecycle.complete_started_operation(
                operation=release_operation,
                attempt=release_attempt,
                summary=f"Created initial deployment release {release.release_id}.",
            )
            superseded_count = _supersede_failed_deployment_setup_executions(
                session=release_session,
                tenant_id=tenant_id,
                project_id=project_id,
                replacement_workflow_id=workflow_id,
            )
            if superseded_count:
                release_session.commit()
            return ProjectDeploymentSetupActivityResult(
                workflow_id=workflow_id,
                tenant_id=tenant_id,
                project_id=project_id,
                status="completed",
                app_ids=(deployment_app.app_id,),
                release_ids=(release.release_id,),
                analysis_run_id=None,
            )
    except HTTPException as exc:
        message = str(exc.detail)
        _mark_setup_failed(workflow_id=workflow_id, message=message)
        raise RuntimeError(message) from exc
    except Exception as exc:
        _mark_setup_failed(workflow_id=workflow_id, message=str(exc))
        raise


def _mark_setup_failed(*, workflow_id: str, message: str) -> None:
    if not workflow_id:
        return
    session_factory = create_session_factory()
    with session_factory() as session:
        workflow = session.get(WorkflowExecution, workflow_id)
        if workflow is None:
            return
        running_pairs = session.execute(
            select(WorkflowOperation, WorkflowOperationAttempt)
            .join(
                WorkflowOperationAttempt,
                WorkflowOperationAttempt.operation_id == WorkflowOperation.operation_id,
            )
            .where(
                WorkflowOperation.workflow_id == workflow_id,
                WorkflowOperation.status == OPERATION_STATUS_RUNNING,
                WorkflowOperationAttempt.status == OPERATION_STATUS_RUNNING,
            )
        ).all()
        for operation, attempt in running_pairs:
            fail_workflow_operation(
                session,
                operation=operation,
                attempt=attempt,
                category="deployment_setup_failure",
                message=message,
            )
        mark_workflow_failed(
            workflow=workflow, message=message, now=datetime.now(timezone.utc)
        )
        session.commit()
