from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from fastapi import HTTPException
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
    failed_release_ids: tuple[str, ...] = ()


_REUSABLE_PREVIEW_RELEASE_STATUSES = {"queued", "provisioning", "deploying", "route_activating", "live"}
_RECONCILE_BEFORE_REUSE_STATUSES = {"provisioning", "deploying", "route_activating"}
_FAILED_PREVIEW_RELEASE_STATUSES = {"failed", "rolled_back"}
_DESTROYED_PREVIEW_STATUS_CONTEXT_KEY = "status_before_destroy"
_TERMINAL_PREVIEW_RELEASE_STATUSES = {"failed", "rolled_back", "live"}
_DEMO_PROOF_LEASE_METADATA_KEY = "demo_proof_lease"
_DEMO_PROOF_LEASE_ACTIVE_STATE = "active"


def create_run_preview_deployment(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    run: Run,
    settings,
    pr_url: str | None = None,
    force: bool = False,
    proof_scope_id: str | None = None,
    demo_proof_lease_required: bool = False,
) -> RunPreviewDeploymentResult:  # noqa: ANN001
    policy = ProjectDeploymentPolicyRead.model_validate(dict(getattr(project, "deployment_config", None) or {}))
    if not policy.enabled:
        return RunPreviewDeploymentResult(created=False, reason="deployments_disabled")
    if not policy.preview_prs_enabled:
        return RunPreviewDeploymentResult(created=False, reason="preview_prs_disabled")

    artifact = latest_pushed_execution_artifact_for_run(session=session, run_id=run.run_id)
    if artifact is None:
        raise RuntimeError("Run preview deployment requires a pushed durable execution branch artifact")

    branch_run_ids = _run_preview_branch_run_ids(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        branch=getattr(run, "branch", None),
        current_run_id=run.run_id,
    )
    normalized_proof_scope_id = _normalize_optional_string(proof_scope_id)
    if demo_proof_lease_required and normalized_proof_scope_id is None:
        normalized_run_id = _normalize_optional_string(run.run_id)
        if normalized_run_id is None:
            raise RuntimeError("Demo proof preview lease requires run_id")
        normalized_proof_scope_id = f"run:{normalized_run_id}:{artifact.commit_sha}"
    if not force:
        if normalized_proof_scope_id is not None:
            scoped_release = _resolve_active_demo_proof_preview_release(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                proof_scope_id=normalized_proof_scope_id,
                expected_commit_sha=artifact.commit_sha,
                current_base_domain=_normalize_optional_string((tenant.deployment_plane_config or {}).get("base_domain")),
            )
            if scoped_release is not None:
                return RunPreviewDeploymentResult(
                    created=False,
                    reason="existing",
                    release=project_deployment_release_to_schema(scoped_release),
                )
        existing_release = _reusable_existing_preview_release(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            branch_run_ids=branch_run_ids,
            expected_commit_sha=artifact.commit_sha,
            current_base_domain=_normalize_optional_string((tenant.deployment_plane_config or {}).get("base_domain")),
        )
        if existing_release is not None:
            if normalized_proof_scope_id is not None:
                _attach_demo_proof_lease_metadata(
                    release=existing_release,
                    proof_scope_id=normalized_proof_scope_id,
                    commit_sha=artifact.commit_sha,
                )
                session.flush()
            return RunPreviewDeploymentResult(
                created=False,
                reason="existing",
                release=project_deployment_release_to_schema(existing_release),
            )
        failed_release = _latest_failed_preview_release_after_retry_limit(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            branch_run_ids=branch_run_ids,
            expected_commit_sha=artifact.commit_sha,
            retry_limit=_preview_failure_retry_limit(settings),
        )
        if failed_release is not None:
            return RunPreviewDeploymentResult(
                created=False,
                reason="preview_failed_retry_limit_reached",
                release=project_deployment_release_to_schema(failed_release),
            )

    _destroy_active_preview_releases_for_branch(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        branch_run_ids=branch_run_ids,
        reason="preview_replaced",
    )

    app = _project_level_deployment_app(session=session, tenant_id=tenant.tenant_id, project_id=project.project_id)
    deployment_config = dict(app.deployment_config or {})
    deployment_config_override: dict[str, object] | None = None
    release_branch = artifact.branch
    release_commit_sha = artifact.commit_sha
    delivery_metadata = _mobile_delivery_metadata(session=session, tenant_id=tenant.tenant_id, project_id=project.project_id)
    if normalized_proof_scope_id is not None:
        delivery_metadata = _delivery_metadata_with_demo_proof_lease(
            delivery_metadata=delivery_metadata,
            proof_scope_id=normalized_proof_scope_id,
            commit_sha=release_commit_sha,
        )
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
            delivery_metadata=delivery_metadata,
        ),
        requested_by_user_id=None,
        deployment_config_override=deployment_config_override,
    )
    return RunPreviewDeploymentResult(created=True, reason="created", release=release)


def _resolve_active_demo_proof_preview_release(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    proof_scope_id: str,
    expected_commit_sha: str,
    current_base_domain: str | None,
) -> ProjectDeploymentRelease | None:
    scoped_releases = _active_demo_proof_preview_releases_for_scope(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        proof_scope_id=proof_scope_id,
    )
    if not scoped_releases:
        return None
    if len(scoped_releases) > 1:
        release_ids = ", ".join(release.release_id for release in scoped_releases)
        raise RuntimeError(
            "Demo proof preview lease scope has multiple active releases and must be reconciled before creating "
            f"another: {proof_scope_id}: {release_ids}"
        )
    scoped_release = scoped_releases[0]
    if (
        str(scoped_release.commit_sha or "").strip() == expected_commit_sha
        and _preview_release_is_reusable_for_current_context(
            session=session,
            release=scoped_release,
            current_base_domain=current_base_domain,
        )
    ):
        return scoped_release
    destroy_project_deployment_preview_release(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        release_id=scoped_release.release_id,
        reason="demo_proof_lease_superseded",
    )
    scoped_release.status = "destroyed"
    scoped_release.destroyed_at = datetime.now(timezone.utc)
    scoped_release.updated_at = scoped_release.destroyed_at
    session.flush()
    return None


def _active_demo_proof_preview_releases_for_scope(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    proof_scope_id: str,
) -> list[ProjectDeploymentRelease]:
    releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.status != "destroyed",
            ProjectDeploymentRelease.destroyed_at.is_(None),
        )
        .order_by(ProjectDeploymentRelease.created_at.asc(), ProjectDeploymentRelease.release_id.asc())
    ).scalars().all()
    return [
        release
        for release in releases
        if _demo_proof_lease_metadata(release).get("proof_scope_id") == proof_scope_id
        and _demo_proof_lease_metadata(release).get("state") == _DEMO_PROOF_LEASE_ACTIVE_STATE
    ]


def _preview_release_is_reusable_for_current_context(
    *,
    session: Session,
    release: ProjectDeploymentRelease,
    current_base_domain: str | None,
) -> bool:
    if str(release.status or "").strip() in _RECONCILE_BEFORE_REUSE_STATUSES:
        reconcile_deployment_release(session=session, release=release)
        session.flush()
        session.refresh(release)
    existing_status = str(release.status or "").strip()
    if existing_status not in _REUSABLE_PREVIEW_RELEASE_STATUSES:
        return False
    if current_base_domain is None:
        return True
    provider_context = release.provider_context if isinstance(release.provider_context, dict) else {}
    provider_base_domain = _normalize_optional_string(provider_context.get("base_domain"))
    if provider_base_domain is not None and provider_base_domain != current_base_domain:
        return False
    return existing_status != "live" or deployment_release_routes_match_base_domain(
        provider_context=provider_context,
        base_domain=current_base_domain,
        require_preview_wildcard_shape=True,
    )


def _demo_proof_lease_metadata(release: ProjectDeploymentRelease) -> dict[str, str]:
    delivery_metadata = release.delivery_metadata if isinstance(release.delivery_metadata, dict) else {}
    raw_metadata = delivery_metadata.get(_DEMO_PROOF_LEASE_METADATA_KEY)
    if not isinstance(raw_metadata, dict):
        return {}
    return {
        "proof_scope_id": str(raw_metadata.get("proof_scope_id") or "").strip(),
        "commit_sha": str(raw_metadata.get("commit_sha") or "").strip(),
        "state": str(raw_metadata.get("state") or "").strip(),
    }


def _delivery_metadata_with_demo_proof_lease(
    *,
    delivery_metadata: dict[str, object],
    proof_scope_id: str,
    commit_sha: str,
) -> dict[str, object]:
    updated = dict(delivery_metadata)
    updated[_DEMO_PROOF_LEASE_METADATA_KEY] = {
        "proof_scope_id": proof_scope_id,
        "commit_sha": commit_sha,
        "state": _DEMO_PROOF_LEASE_ACTIVE_STATE,
    }
    return updated


def _attach_demo_proof_lease_metadata(
    *,
    release: ProjectDeploymentRelease,
    proof_scope_id: str,
    commit_sha: str,
) -> None:
    release.delivery_metadata = _delivery_metadata_with_demo_proof_lease(
        delivery_metadata=release.delivery_metadata if isinstance(release.delivery_metadata, dict) else {},
        proof_scope_id=proof_scope_id,
        commit_sha=commit_sha,
    )
    release.updated_at = datetime.now(timezone.utc)


def _reusable_existing_preview_release(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    branch_run_ids: tuple[str, ...],
    expected_commit_sha: str,
    current_base_domain: str | None,
) -> ProjectDeploymentRelease | None:
    if not branch_run_ids:
        return None
    existing_release = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.source_run_id.in_(branch_run_ids),
            ProjectDeploymentRelease.commit_sha == expected_commit_sha,
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
    branch_run_ids: tuple[str, ...],
    expected_commit_sha: str,
    retry_limit: int,
) -> ProjectDeploymentRelease | None:
    if not branch_run_ids:
        return None
    preview_releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.source_run_id.in_(branch_run_ids),
            ProjectDeploymentRelease.commit_sha == expected_commit_sha,
        )
        .order_by(ProjectDeploymentRelease.created_at.desc(), ProjectDeploymentRelease.release_id.desc())
    ).scalars().all()
    failed_releases = [release for release in preview_releases if _preview_release_counts_as_failed_attempt(release)]
    if len(failed_releases) < retry_limit:
        return None
    return failed_releases[0]


def _preview_release_counts_as_failed_attempt(release: ProjectDeploymentRelease) -> bool:
    status = str(release.status or "").strip()
    if status in _FAILED_PREVIEW_RELEASE_STATUSES:
        return True
    if status != "destroyed":
        return False
    provider_context = release.provider_context if isinstance(release.provider_context, dict) else {}
    return str(provider_context.get(_DESTROYED_PREVIEW_STATUS_CONTEXT_KEY) or "").strip() in _FAILED_PREVIEW_RELEASE_STATUSES


def _destroy_active_preview_releases_for_branch(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    branch_run_ids: tuple[str, ...],
    reason: str,
) -> PreviewCleanupResult:
    if not branch_run_ids:
        return PreviewCleanupResult(destroyed_release_ids=())
    releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.source_run_id.in_(branch_run_ids),
            ProjectDeploymentRelease.status != "destroyed",
        )
        .order_by(ProjectDeploymentRelease.created_at.asc(), ProjectDeploymentRelease.release_id.asc())
    ).scalars().all()
    destroyed: list[str] = []
    for release in releases:
        status = str(release.status or "").strip()
        if status not in _REUSABLE_PREVIEW_RELEASE_STATUSES and not _preview_release_has_provider_resource(release):
            continue
        destroyed_release = destroy_project_deployment_preview_release(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            release_id=release.release_id,
            reason=reason,
        )
        destroyed.append(destroyed_release.release_id)
    return PreviewCleanupResult(destroyed_release_ids=tuple(destroyed))


def _run_preview_branch_run_ids(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    branch: str | None,
    current_run_id: str,
) -> tuple[str, ...]:
    normalized_current_run_id = _normalize_optional_string(current_run_id)
    if normalized_current_run_id is None:
        return ()
    normalized_branch = _normalize_optional_string(branch)
    if normalized_branch is None:
        return (normalized_current_run_id,)
    run_ids = list(
        session.execute(
            select(Run.run_id)
            .where(
                Run.tenant_id == tenant_id,
                Run.project_id == project_id,
                Run.branch == normalized_branch,
            )
            .order_by(Run.created_at.asc(), Run.run_id.asc())
        ).scalars()
    )
    if normalized_current_run_id not in run_ids:
        run_ids.append(normalized_current_run_id)
    return tuple(dict.fromkeys(run_ids))


def _preview_release_has_provider_resource(release: ProjectDeploymentRelease) -> bool:
    provider_context = release.provider_context if isinstance(release.provider_context, dict) else {}
    for key in ("application_uuid", "service_uuid"):
        if str(provider_context.get(key) or "").strip():
            return True
    return False


def cleanup_stale_run_preview_deployments(
    *,
    session: Session,
    settings,
    limit: int = 25,
    now: datetime | None = None,
    exclude_release_ids: set[str] | None = None,
) -> PreviewCleanupResult:  # noqa: ANN001
    cutoff = _run_preview_ttl_cutoff(settings=settings, now=now or datetime.now(timezone.utc))
    excluded = set(exclude_release_ids or set())
    releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.release_kind == "run_preview",
            ProjectDeploymentRelease.status.in_(_TERMINAL_PREVIEW_RELEASE_STATUSES),
            ProjectDeploymentRelease.destroyed_at.is_(None),
        )
        .order_by(ProjectDeploymentRelease.created_at.asc(), ProjectDeploymentRelease.release_id.asc())
        .limit(max(1, int(limit)))
    ).scalars().all()
    destroyed: list[str] = []
    failed: list[str] = []
    for release in releases:
        if release.release_id in excluded:
            continue
        if not _run_preview_release_is_cleanup_due(release=release, ttl_cutoff=cutoff):
            continue
        try:
            destroyed_release = destroy_project_deployment_preview_release(
                session=session,
                tenant_id=release.tenant_id,
                project_id=release.project_id,
                release_id=release.release_id,
                reason="preview_ttl_expired" if str(release.status or "").strip() == "live" else "preview_terminal_cleanup",
            )
        except HTTPException as exc:
            session.rollback()
            current = session.get(ProjectDeploymentRelease, release.release_id)
            if current is not None:
                current.last_error = f"Preview cleanup failed: {exc.detail}"
                current.updated_at = datetime.now(timezone.utc)
                session.flush()
            failed.append(release.release_id)
            continue
        destroyed.append(destroyed_release.release_id)
    return PreviewCleanupResult(destroyed_release_ids=tuple(destroyed), failed_release_ids=tuple(failed))


def _run_preview_ttl_cutoff(*, settings, now: datetime) -> datetime:  # noqa: ANN001
    try:
        ttl_seconds = max(60, int(getattr(settings, "run_preview_release_ttl_seconds", 86400) or 86400))
    except (TypeError, ValueError):
        ttl_seconds = 86400
    return _as_utc(now) - timedelta(seconds=ttl_seconds)


def _run_preview_release_is_cleanup_due(*, release: ProjectDeploymentRelease, ttl_cutoff: datetime) -> bool:
    status = str(release.status or "").strip()
    if status in _FAILED_PREVIEW_RELEASE_STATUSES:
        return True
    if status != "live":
        return False
    anchor = release.completed_at or release.updated_at or release.created_at
    if anchor is None:
        return False
    return _as_utc(anchor) <= ttl_cutoff


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
            "note": (
                "Mobile QA demo proof uses source-built simulator/emulator recordings against the preview "
                "release context. Fastlane distribution is only required for external mobile delivery."
            ),
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
