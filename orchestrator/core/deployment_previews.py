from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.admin.deployment_release_service import create_project_deployment_release
from orchestrator.api.admin.deployment_release_service import deployment_release_routes_match_base_domain
from orchestrator.api.admin.deployment_release_service import destroy_project_deployment_preview_release
from orchestrator.api.admin.deployment_release_service import project_deployment_release_to_schema
from orchestrator.api.deployment_schemas import ProjectDeploymentPolicyRead
from orchestrator.api.schemas import ProjectDeploymentReleaseCreate, ProjectDeploymentReleaseRead
from orchestrator.core.deployment_runtime import reconcile_deployment_release
from orchestrator.core.deployment_setup.artifacts import (
    checkout_branch_commit,
    ensure_deployment_compose_artifact,
    run_git,
)
from orchestrator.core.deployment_setup.compose_normalizer import normalize_generated_compose_for_coolify
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


_REUSABLE_PREVIEW_RELEASE_STATUSES = {"queued", "provisioning", "deploying", "route_activating", "live"}
_RECONCILE_BEFORE_REUSE_STATUSES = {"provisioning", "deploying", "route_activating"}
_FAILED_PREVIEW_RELEASE_STATUSES = {"failed", "rolled_back"}


def create_run_preview_deployment(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    run: Run,
    settings,
    pr_url: str | None = None,
    force: bool = False,
) -> RunPreviewDeploymentResult:  # noqa: ANN001
    policy = ProjectDeploymentPolicyRead.model_validate(dict(getattr(project, "deployment_config", None) or {}))
    if not policy.enabled:
        return RunPreviewDeploymentResult(created=False, reason="deployments_disabled")
    if not policy.preview_prs_enabled:
        return RunPreviewDeploymentResult(created=False, reason="preview_prs_disabled")

    if not force:
        existing_release = _reusable_existing_preview_release(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            run_id=run.run_id,
            current_base_domain=_normalize_optional_string((tenant.deployment_plane_config or {}).get("base_domain")),
        )
        if existing_release is not None:
            return RunPreviewDeploymentResult(
                created=False,
                reason="existing",
                release=project_deployment_release_to_schema(existing_release),
            )
        failed_release = _latest_failed_preview_release_after_retry_limit(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            run_id=run.run_id,
            retry_limit=_preview_failure_retry_limit(settings),
        )
        if failed_release is not None:
            return RunPreviewDeploymentResult(
                created=False,
                reason="preview_failed_retry_limit_reached",
                release=project_deployment_release_to_schema(failed_release),
            )

    _destroy_active_preview_releases_for_run(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        run_id=run.run_id,
        reason="preview_replaced",
    )

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
            compose_raw=normalize_generated_compose_for_coolify(generated_compose_raw).compose_raw,
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


def _reusable_existing_preview_release(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    run_id: str,
    current_base_domain: str | None,
) -> ProjectDeploymentRelease | None:
    existing_release = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.source_run_id == run_id,
            ProjectDeploymentRelease.status.in_(_REUSABLE_PREVIEW_RELEASE_STATUSES),
            ProjectDeploymentRelease.destroyed_at.is_(None),
        )
        .order_by(ProjectDeploymentRelease.created_at.desc(), ProjectDeploymentRelease.release_id.desc())
    ).scalars().first()
    if existing_release is None:
        return None
    if str(existing_release.status or "").strip() in _RECONCILE_BEFORE_REUSE_STATUSES:
        reconcile_deployment_release(session=session, release=existing_release)
        session.flush()
        session.refresh(existing_release)
    existing_status = str(existing_release.status or "").strip()
    if existing_status not in _REUSABLE_PREVIEW_RELEASE_STATUSES:
        return None
    if current_base_domain is not None:
        provider_context = existing_release.provider_context if isinstance(existing_release.provider_context, dict) else {}
        provider_base_domain = _normalize_optional_string(provider_context.get("base_domain"))
        if provider_base_domain is not None and provider_base_domain != current_base_domain:
            return None
        if existing_status == "live" and not deployment_release_routes_match_base_domain(
            provider_context=provider_context,
            base_domain=current_base_domain,
            require_preview_wildcard_shape=True,
        ):
            return None
    return existing_release


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _preview_failure_retry_limit(settings) -> int:  # noqa: ANN001
    try:
        raw_value = getattr(settings, "qa_demo_max_attempts", 3)
        return max(1, int(raw_value or 3))
    except (TypeError, ValueError):
        return 3


def _latest_failed_preview_release_after_retry_limit(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    run_id: str,
    retry_limit: int,
) -> ProjectDeploymentRelease | None:
    failed_releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.source_run_id == run_id,
            ProjectDeploymentRelease.status.in_(_FAILED_PREVIEW_RELEASE_STATUSES),
            ProjectDeploymentRelease.destroyed_at.is_(None),
        )
        .order_by(ProjectDeploymentRelease.created_at.desc(), ProjectDeploymentRelease.release_id.desc())
    ).scalars().all()
    if len(failed_releases) < retry_limit:
        return None
    return failed_releases[0]


def _destroy_active_preview_releases_for_run(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    run_id: str,
    reason: str,
) -> PreviewCleanupResult:
    releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.source_run_id == run_id,
            ProjectDeploymentRelease.status.in_(_REUSABLE_PREVIEW_RELEASE_STATUSES),
            ProjectDeploymentRelease.destroyed_at.is_(None),
        )
        .order_by(ProjectDeploymentRelease.created_at.asc(), ProjectDeploymentRelease.release_id.asc())
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
