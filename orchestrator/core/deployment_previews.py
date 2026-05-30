from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.admin.deployment_release_service import create_project_deployment_release
from orchestrator.api.admin.deployment_release_service import destroy_project_deployment_preview_release
from orchestrator.api.deployment_schemas import ProjectDeploymentPolicyRead
from orchestrator.api.schemas import ProjectDeploymentReleaseCreate, ProjectDeploymentReleaseRead
from orchestrator.core.deployment_setup.artifacts import (
    checkout_branch_commit,
    ensure_deployment_compose_artifact,
    run_git,
)
from orchestrator.core.deployment_setup.compose_normalizer import normalize_compose_for_coolify
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.workflow.execution_artifacts import latest_pushed_execution_artifact_for_run
from orchestrator.storage.models import Project, ProjectApp, Run, Tenant
from orchestrator.storage.models import ProjectDeploymentRelease
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.project_repo_checkout import project_repo_dir


@dataclass(frozen=True)
class RunPreviewDeploymentResult:
    created: bool
    reason: str
    release: ProjectDeploymentReleaseRead | None = None


@dataclass(frozen=True)
class PreviewCleanupResult:
    destroyed_release_ids: tuple[str, ...]



def create_run_preview_deployment(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    run: Run,
    settings,
    pr_url: str | None = None,
) -> RunPreviewDeploymentResult:  # noqa: ANN001
    policy = ProjectDeploymentPolicyRead.model_validate(dict(getattr(project, "deployment_config", None) or {}))
    if not policy.enabled:
        return RunPreviewDeploymentResult(created=False, reason="deployments_disabled")
    if not policy.preview_prs_enabled:
        return RunPreviewDeploymentResult(created=False, reason="preview_prs_disabled")

    artifact = latest_pushed_execution_artifact_for_run(session=session, run_id=run.run_id)
    if artifact is None:
        raise RuntimeError("Run preview deployment requires a pushed durable execution branch artifact")

    app = _project_level_deployment_app(session=session, tenant_id=tenant.tenant_id, project_id=project.project_id)
    deployment_config = dict(app.deployment_config or {})
    deployment_config_override: dict[str, object] | None = None
    release_branch = artifact.branch
    release_commit_sha = artifact.commit_sha
    if str(deployment_config.get("source_strategy") or app.build_strategy or "").strip() == "docker_compose":
        generated_compose_raw = str(deployment_config.get("generated_compose_raw") or "").strip()
        if not generated_compose_raw:
            raise RuntimeError("Run preview deployment requires generated Docker Compose content from deployment setup")
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
        checkout_path = project_repo_dir(
            base_dir=str(getattr(settings, "project_repo_checkout_base_dir", "") or ""),
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        )
        run_git(["fetch", "origin", artifact.branch], cwd=checkout_path, token=github_client.get_installation_token())
        checkout_branch_commit(repo_dir=checkout_path, branch=artifact.branch, commit_sha=artifact.commit_sha)
        release_branch, release_commit_sha, deployment_compose_path = ensure_deployment_compose_artifact(
            repo_dir=checkout_path,
            project_id=project.project_id,
            source_branch=artifact.branch,
            source_commit_sha=artifact.commit_sha,
            compose_raw=normalize_compose_for_coolify(generated_compose_raw).compose_raw,
            npm_service_source_paths=_npm_service_source_paths(deployment_config),
            token=github_client.get_installation_token(),
        )
        deployment_config_override = {
            "deployment_branch": release_branch,
            "deployment_commit_sha": release_commit_sha,
            "deployment_compose_path": deployment_compose_path,
        }

    release = create_project_deployment_release(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        payload=ProjectDeploymentReleaseCreate(
            app_id=app.app_id,
            release_kind="run_preview",
            git_ref=release_branch,
            commit_sha=release_commit_sha,
            reason=f"Preview for run {run.run_id}",
            source_run_id=run.run_id,
            pr_number=_pr_number_from_run(run, pr_url=pr_url),
            delivery_metadata=_mobile_delivery_metadata(session=session, tenant_id=tenant.tenant_id, project_id=project.project_id),
        ),
        requested_by_user_id=None,
        deployment_config_override=deployment_config_override,
    )
    return RunPreviewDeploymentResult(created=True, reason="created", release=release)


def destroy_run_preview_deployments_for_pr(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    pr_number: int,
    reason: str,
) -> PreviewCleanupResult:
    releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.pr_number == pr_number,
            ProjectDeploymentRelease.status != "destroyed",
        )
        .order_by(ProjectDeploymentRelease.created_at.asc())
    ).scalars().all()
    destroyed: list[str] = []
    for release in releases:
        destroyed_release = destroy_project_deployment_preview_release(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            release_id=release.release_id,
            reason=reason,
        )
        destroyed.append(destroyed_release.release_id)
    return PreviewCleanupResult(destroyed_release_ids=tuple(destroyed))


def _project_level_deployment_app(*, session: Session, tenant_id: str, project_id: str) -> ProjectApp:
    app = session.execute(
        select(ProjectApp)
        .where(
            ProjectApp.tenant_id == tenant_id,
            ProjectApp.project_id == project_id,
            ProjectApp.source_path == ".",
        )
        .order_by(ProjectApp.created_at.asc(), ProjectApp.app_id.asc())
    ).scalars().first()
    if app is None:
        raise RuntimeError("Run preview deployment requires a project-level deployment app")
    return app


def _npm_service_source_paths(deployment_config: dict[str, object]) -> tuple[str, ...]:
    deployment_plan = deployment_config.get("deployment_plan")
    if not isinstance(deployment_plan, dict):
        return ()
    services = deployment_plan.get("services")
    if not isinstance(services, list):
        return ()
    source_paths = []
    for service in services:
        if not isinstance(service, dict):
            continue
        if str(service.get("build_strategy") or "").strip() != "npm":
            continue
        source_path = str(service.get("source_path") or "").strip()
        if source_path:
            source_paths.append(source_path)
    return tuple(source_paths)


def _mobile_delivery_metadata(*, session: Session, tenant_id: str, project_id: str) -> dict[str, object]:
    mobile_apps = session.execute(
        select(ProjectApp).where(
            ProjectApp.tenant_id == tenant_id,
            ProjectApp.project_id == project_id,
            ProjectApp.source_path != ".",
        )
    ).scalars().all()
    mobile_targets = []
    for app in mobile_apps:
        descriptor = " ".join(
            str(value or "").lower()
            for value in (app.detected_runtime, app.detected_language, app.build_strategy, app.name)
        )
        if any(marker in descriptor for marker in ("ios", "android", "react native", "flutter", "mobile")):
            mobile_targets.append({"app_id": app.app_id, "name": app.name, "source_path": app.source_path})
    if not mobile_targets:
        return {}
    return {
        "mobile_delivery": {
            "status": "pending_fastlane_distribution",
            "targets": mobile_targets,
            "note": "Mobile apps are qualified through the preview API/backend deployment and require configured Fastlane distribution before device testing.",
        }
    }


def _pr_number_from_run(run: Run, *, pr_url: str | None) -> int | None:
    plan = getattr(run, "plan", None)
    if isinstance(plan, dict):
        context = plan.get("context")
        if isinstance(context, dict):
            execution_context = context.get("execution_context")
            if isinstance(execution_context, dict):
                value = execution_context.get("pr_number")
                if isinstance(value, int) and value > 0:
                    return value
    resolved_pr_url = str(pr_url or getattr(run, "pr_url", "") or "").strip()
    if resolved_pr_url:
        try:
            last_part = Path(urlparse(resolved_pr_url).path).name
            parsed = int(last_part)
            return parsed if parsed > 0 else None
        except ValueError:
            return None
    return None
