from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.core.config import get_settings
from orchestrator.core.integrations.atlassian.links import tenant_jira_issue_url
from orchestrator.core.deployment_setup.compose_normalizer import (
    CoolifyComposeNormalizationResult,
    normalize_generated_compose_for_coolify,
    normalize_compose_for_coolify,
)
from orchestrator.core.local_preview_route_sync import ensure_local_preview_route_cleanup_command
from orchestrator.core.node_release_contracts import (
    LEGACY_EXPO_CLI_INSTALL_COMMAND,
    LEGACY_EXPO_WEB_START_COMMAND,
    NODE_INSTALL_WITH_LEGACY_PEERS_COMMAND,
)
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.secret_manager import normalize_secret_ref, resolve_scoped_secret_ref
from orchestrator.api.admin.deployment_config_service import (
    get_project_default_app,
    get_project_app,
    project_deployment_config_to_schema,
    tenant_deployment_plane_to_schema,
)
from orchestrator.api.schemas import (
    ProjectDeploymentConfigRead,
    ProjectDeploymentReleaseCreate,
    ProjectDeploymentReleaseLogsRead,
    ProjectDeploymentReleaseRead,
    ProjectDeploymentReleaseStatusUpdate,
    TenantDeploymentPlaneRead,
)
from orchestrator.api.deployment_schemas import (
    ProjectDeploymentPolicyRead,
    ProjectDeploymentServiceUrlRead,
    redact_deployment_config_secrets,
)
from orchestrator.storage.models import Project, ProjectApp, ProjectDeploymentRelease, Run, Tenant
from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig, CoolifyApiError
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config

_ACTIVE_DEPLOYMENT_PLANE_STATES = {"active", "degraded"}
_QUEUED_RELEASE_STATUS = "queued"
_RELEASE_ALLOWED_TRANSITIONS = {
    "queued": {"provisioning", "deploying", "route_activating", "live", "failed", "rolled_back", "destroyed"},
    "provisioning": {"deploying", "route_activating", "live", "failed", "rolled_back", "destroyed"},
    "deploying": {"route_activating", "live", "failed", "destroyed"},
    "route_activating": {"deploying", "live", "failed", "rolled_back", "destroyed"},
    "live": {"rolled_back", "destroyed"},
    "failed": {"destroyed"},
    "rolled_back": {"destroyed"},
    "destroyed": set(),
}
_MAX_PROVIDER_LOG_CHARS = 60_000


@dataclass(frozen=True)
class PreparedDeploymentRelease:
    provider: str
    environment_name: str | None
    source_strategy: str | None
    deployment_snapshot: dict[str, object]
    provider_context: dict[str, object]


@dataclass(frozen=True)
class RouteVerificationResult:
    ok: bool
    error: str | None = None


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _route_binding_external_url(value: dict[str, object]) -> str | None:
    scheme = _normalize_optional_string(value.get("scheme")) or "https"
    host = _normalize_optional_string(value.get("host"))
    if host is None:
        return None
    path = _normalize_optional_string(value.get("path")) or ""
    proxy_port_value = value.get("proxy_port")
    proxy_port = proxy_port_value if isinstance(proxy_port_value, int) else None
    netloc = f"{host}:{proxy_port}" if proxy_port is not None else host
    return f"{scheme}://{netloc}{path}"


def _normalize_route_binding(value: object, *, release_id: str, default_status: str = "active") -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    service_key = _normalize_optional_string(value.get("service_key"))
    service_name = _normalize_optional_string(value.get("service_name")) or service_key
    host = _normalize_optional_string(value.get("host"))
    url = _route_binding_external_url(value)
    internal_url = _normalize_optional_string(value.get("internal_url"))
    url_kind = _normalize_optional_string(value.get("url_kind")) or "generated"
    service_kind = _normalize_optional_string(value.get("service_kind")) or "website"
    status_value = _normalize_optional_string(value.get("status")) or default_status
    if service_key is None or service_name is None or host is None or url is None:
        return None
    port_value = value.get("port")
    try:
        port = int(port_value) if port_value is not None else None
    except (TypeError, ValueError):
        port = None
    domain_key = _normalize_optional_string(value.get("domain_key"))
    proxy_port_value = value.get("proxy_port")
    proxy_port = proxy_port_value if isinstance(proxy_port_value, int) else None
    return ProjectDeploymentServiceUrlRead(
        service_key=service_key,
        service_name=service_name,
        service_kind="api" if service_kind == "api" else "website",
        url=url,
        url_kind="custom" if url_kind == "custom" else "generated",
        internal_url=internal_url,
        status=status_value if status_value in {"pending", "active", "failed"} else default_status,
        port=port,
        proxy_port=proxy_port,
        host=host,
        domain_key=domain_key,
        release_id=release_id,
    ).model_dump(exclude_none=True)


def _release_service_urls(release: ProjectDeploymentRelease) -> list[ProjectDeploymentServiceUrlRead]:
    provider_context = _coerce_dict(release.provider_context)
    raw_route_bindings = provider_context.get("route_bindings")
    normalized: list[dict[str, object]] = []
    release_is_live = str(release.status or "").strip() == "live"
    release_failed = str(release.status or "").strip() in {"failed", "rolled_back"}
    if isinstance(raw_route_bindings, list):
        for item in raw_route_bindings:
            service_url = _normalize_route_binding(
                item,
                release_id=release.release_id,
                default_status="failed" if release_failed else "active" if release_is_live else "pending",
            )
            if service_url is not None:
                if release_is_live and service_url.get("status") == "pending":
                    service_url["status"] = "active"
                if release_failed:
                    service_url["status"] = "failed"
                normalized.append(service_url)
    return [ProjectDeploymentServiceUrlRead.model_validate(item) for item in normalized]


def verify_release_route_bindings(
    release: ProjectDeploymentRelease,
    *,
    fetch_url_fn=None,  # noqa: ANN001
) -> RouteVerificationResult:
    service_urls = _release_service_urls(release)
    if not service_urls:
        return RouteVerificationResult(ok=True)
    for service_url in service_urls:
        result = fetch_url_fn(service_url.url) if fetch_url_fn is not None else _fetch_route_activation(service_url)
        if result is not None:
            return RouteVerificationResult(ok=False, error=f"{service_url.url}: {result}")
    return RouteVerificationResult(ok=True)


def _fetch_route_activation(service_url: ProjectDeploymentServiceUrlRead) -> str | None:
    probe_url = service_url.internal_url or service_url.url
    headers = {"User-Agent": "master-builder-route-verifier/1.0"}
    if service_url.internal_url and service_url.host:
        headers["Host"] = service_url.host
    request = Request(probe_url, method="GET", headers=headers)
    try:
        with urlopen(request, timeout=5) as response:  # noqa: S310 - URL is a release route generated by MB config.
            status_code = int(getattr(response, "status", 0) or 0)
            if 200 <= status_code < 500:
                return None
            return f"HTTP {status_code}"
    except HTTPError as exc:
        body = exc.read(256).decode("utf-8", errors="replace").strip()
        if exc.code == 404 and body == "404 page not found":
            return "provider route not active"
        if 400 <= exc.code < 500:
            return None
        return f"HTTP {exc.code}: {body}"
    except URLError as exc:
        return str(exc.reason)
    except TimeoutError:
        return "route verification timed out"


def _resolve_project_app_scope(
    *,
    session,
    tenant_id: str,
    project: Project,
    app_id: str | None,
) -> ProjectApp:  # noqa: ANN001
    normalized_app_id = _normalize_optional_string(app_id)
    if normalized_app_id is not None:
        app = get_project_app(
            session=session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            app_id=normalized_app_id,
        )
        if app is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")
        return app
    app = get_project_default_app(session=session, tenant_id=tenant_id, project_id=project.project_id)
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app not found")
    return app


def _release_scope_filter(
    *,
    project_app: ProjectApp,
) -> tuple[object, ...]:
    return (ProjectDeploymentRelease.app_id == project_app.app_id,)


def project_deployment_release_to_schema(
    release: ProjectDeploymentRelease,
    *,
    source_run: Run | None = None,
    source_issue_url: str | None = None,
) -> ProjectDeploymentReleaseRead:
    return ProjectDeploymentReleaseRead(
        release_id=release.release_id,
        tenant_id=release.tenant_id,
        project_id=release.project_id,
        app_id=release.app_id,
        provider=release.provider,
        release_kind=release.release_kind,
        status=release.status,
        environment_name=release.environment_name,
        source_strategy=release.source_strategy,
        git_ref=release.git_ref,
        commit_sha=release.commit_sha,
        release_name=deployment_release_name(git_ref=release.git_ref, commit_sha=release.commit_sha),
        source_run_id=release.source_run_id,
        source_issue_key=source_run.issue_key if source_run is not None else None,
        source_issue_summary=source_run.issue_summary if source_run is not None else None,
        source_issue_url=source_issue_url,
        pr_number=release.pr_number,
        requested_by_user_id=release.requested_by_user_id,
        deployment_snapshot=redact_deployment_config_secrets(_coerce_dict(release.deployment_snapshot)),
        provider_context=_coerce_dict(release.provider_context),
        delivery_metadata=_coerce_dict(release.delivery_metadata),
        service_urls=_release_service_urls(release),
        last_error=release.last_error,
        requested_at=release.requested_at,
        started_at=release.started_at,
        completed_at=release.completed_at,
        destroyed_at=release.destroyed_at,
        created_at=release.created_at,
        updated_at=release.updated_at,
    )


def deployment_release_name(*, git_ref: str, commit_sha: str) -> str:
    normalized_ref = _normalize_git_branch(git_ref)
    normalized_sha = _normalize_optional_string(commit_sha)
    if normalized_sha is None:
        raise ValueError("Deployment release requires commit_sha")
    return f"{normalized_ref} @ {normalized_sha[:8]}"


def _source_runs_by_id_for_releases(
    *,
    session,
    tenant_id: str,
    releases: list[ProjectDeploymentRelease],
) -> dict[str, Run]:  # noqa: ANN001
    source_run_ids = sorted(
        {
            str(release.source_run_id or "").strip()
            for release in releases
            if str(release.source_run_id or "").strip()
        }
    )
    if not source_run_ids:
        return {}
    rows = session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.run_id.in_(source_run_ids),
        )
    ).scalars().all()
    return {run.run_id: run for run in rows}


def _source_issue_url(*, session, tenant: Tenant, source_run: Run | None) -> str | None:  # noqa: ANN001
    if source_run is None:
        return None
    return tenant_jira_issue_url(session=session, tenant=tenant, issue_key=source_run.issue_key)


def _project_deployment_release_to_schema_with_source(
    *,
    session,
    tenant: Tenant,
    release: ProjectDeploymentRelease,
) -> ProjectDeploymentReleaseRead:  # noqa: ANN001
    source_run = session.get(Run, release.source_run_id) if release.source_run_id is not None else None
    if source_run is not None and source_run.tenant_id != release.tenant_id:
        source_run = None
    return project_deployment_release_to_schema(
        release,
        source_run=source_run,
        source_issue_url=_source_issue_url(session=session, tenant=tenant, source_run=source_run),
    )


def list_project_deployment_releases(
    *,
    session,
    tenant_id: str,
    project_id: str,
    app_id: str | None = None,
) -> list[ProjectDeploymentReleaseRead]:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project_app = _resolve_project_app_scope(
        session=session,
        tenant_id=tenant_id,
        project=project,
        app_id=app_id,
    )

    releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            *_release_scope_filter(project_app=project_app),
        )
        .order_by(ProjectDeploymentRelease.created_at.desc())
    ).scalars().all()
    source_runs_by_id = _source_runs_by_id_for_releases(session=session, tenant_id=tenant_id, releases=list(releases))
    return [
        project_deployment_release_to_schema(
            release,
            source_run=source_runs_by_id.get(str(release.source_run_id or "").strip()),
            source_issue_url=_source_issue_url(
                session=session,
                tenant=tenant,
                source_run=source_runs_by_id.get(str(release.source_run_id or "").strip()),
            ),
        )
        for release in releases
    ]


def get_project_deployment_release(
    *,
    session,
    tenant_id: str,
    project_id: str,
    release_id: str,
    app_id: str | None = None,
) -> ProjectDeploymentReleaseRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project_app = _resolve_project_app_scope(
        session=session,
        tenant_id=tenant_id,
        project=project,
        app_id=app_id,
    )
    release = session.get(ProjectDeploymentRelease, release_id)
    if release is None or release.tenant_id != tenant_id or release.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    release_app_id = _normalize_optional_string(release.app_id)
    if project_app.source_path == ".":
        if release_app_id is not None and release_app_id != project_app.app_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    elif release_app_id != project_app.app_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    return _project_deployment_release_to_schema_with_source(session=session, tenant=tenant, release=release)


def get_project_deployment_release_logs(
    *,
    session,
    tenant_id: str,
    project_id: str,
    release_id: str,
    app_id: str | None = None,
) -> ProjectDeploymentReleaseLogsRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project_app = _resolve_project_app_scope(
        session=session,
        tenant_id=tenant_id,
        project=project,
        app_id=app_id,
    )
    release = session.get(ProjectDeploymentRelease, release_id)
    if release is None or release.tenant_id != tenant_id or release.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    release_app_id = _normalize_optional_string(release.app_id)
    if project_app.source_path == ".":
        if release_app_id is not None and release_app_id != project_app.app_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    elif release_app_id != project_app.app_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")

    if release.provider != "internal_coolify":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Deployment release is not backed by Coolify")

    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    client = _coolify_client_for_release(session=session, tenant=tenant, project=project, tenant_plane=tenant_plane)
    provider_context = _coerce_dict(release.provider_context)
    deployment_uuid = _normalize_optional_string(provider_context.get("deployment_uuid"))
    application_uuid = _normalize_optional_string(provider_context.get("application_uuid"))
    service_uuid = _normalize_optional_string(provider_context.get("service_uuid"))
    try:
        payload = _coolify_deployment_payload_for_logs(
            client=client,
            deployment_uuid=deployment_uuid,
            application_uuid=application_uuid,
            service_uuid=service_uuid,
        )
    except CoolifyApiError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Coolify logs unavailable: {exc}") from exc

    logs, truncated = _bounded_provider_logs(_logs_from_provider_payload(payload))
    application_payload = _coerce_dict(payload.get("application"))
    return ProjectDeploymentReleaseLogsRead(
        provider=release.provider,
        release_id=release.release_id,
        deployment_uuid=_normalize_optional_string(payload.get("deployment_uuid")) or deployment_uuid,
        application_uuid=(
            _normalize_optional_string(payload.get("application_id"))
            or _normalize_optional_string(application_payload.get("uuid"))
            or application_uuid
        ),
        status=_normalize_optional_string(payload.get("status")),
        logs=logs,
        truncated=truncated,
        fetched_at=datetime.now(timezone.utc),
    )


def create_project_deployment_release(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentReleaseCreate,
    requested_by_user_id: str | None,
    deployment_config_override: dict[str, object] | None = None,
) -> ProjectDeploymentReleaseRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    project_app = _resolve_project_app_scope(
        session=session,
        tenant_id=tenant_id,
        project=project,
        app_id=payload.app_id,
    )
    project_deployment = _project_deployment_config_for_release(
        project_app=project_app,
        deployment_config_override=deployment_config_override,
    )
    prepared_release = prepare_project_deployment_release(
        tenant=tenant,
        project=project,
        project_app=project_app,
        tenant_plane=tenant_plane,
        project_deployment=project_deployment,
        payload=payload,
    )
    latest_releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            *_release_scope_filter(project_app=project_app),
            ProjectDeploymentRelease.provider == prepared_release.provider,
            ProjectDeploymentRelease.release_kind == payload.release_kind,
            *(
                [ProjectDeploymentRelease.source_run_id == payload.source_run_id]
                if payload.release_kind == "run_preview" and payload.source_run_id is not None
                else []
            ),
        )
        .order_by(ProjectDeploymentRelease.created_at.desc())
    ).scalars().all()
    existing_application_uuid = None
    existing_service_uuid = None
    for latest_release in latest_releases:
        latest_provider_context = _coerce_dict(latest_release.provider_context)
        if payload.release_kind == "run_preview" and not _run_preview_release_can_reuse_existing_application(
            provider_context=latest_provider_context,
            tenant_plane=tenant_plane,
        ):
            continue
        existing_application_uuid = str(latest_provider_context.get("application_uuid") or "").strip() or None
        existing_service_uuid = str(latest_provider_context.get("service_uuid") or "").strip() or None
        if existing_application_uuid or existing_service_uuid:
            break

    now = datetime.now(timezone.utc)
    release = ProjectDeploymentRelease(
        release_id=str(uuid4()),
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=project_app.app_id,
        provider=prepared_release.provider,
        release_kind=payload.release_kind,
        status=_QUEUED_RELEASE_STATUS,
        environment_name=prepared_release.environment_name,
        source_strategy=prepared_release.source_strategy,
        git_ref=_normalize_git_branch(payload.git_ref),
        commit_sha=_normalize_optional_string(payload.commit_sha),
        source_run_id=_normalize_optional_string(payload.source_run_id),
        pr_number=payload.pr_number,
        requested_by_user_id=_normalize_optional_string(requested_by_user_id),
        deployment_snapshot=prepared_release.deployment_snapshot,
        provider_context=prepared_release.provider_context,
        delivery_metadata=dict(payload.delivery_metadata or {}),
        last_error=None,
        requested_at=now,
        started_at=None,
        completed_at=None,
        destroyed_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(release)
    session.commit()

    try:
        provider_submission = submit_internal_coolify_release(
            session=session,
            tenant=tenant,
            project=project,
            project_app=project_app,
            tenant_plane=tenant_plane,
            project_deployment=project_deployment,
            payload=payload,
            existing_application_uuid=existing_application_uuid,
            existing_service_uuid=existing_service_uuid,
        )
    except HTTPException as exc:
        failure_time = datetime.now(timezone.utc)
        release.status = "failed"
        release.last_error = str(exc.detail)
        release.completed_at = failure_time
        release.updated_at = failure_time
        if payload.release_kind == "production":
            project_app.status = "failed"
            project_app.updated_at = failure_time
        session.commit()
        raise
    except Exception as exc:
        failure_time = datetime.now(timezone.utc)
        release.status = "failed"
        release.last_error = str(exc)
        release.completed_at = failure_time
        release.updated_at = failure_time
        if payload.release_kind == "production":
            project_app.status = "failed"
            project_app.updated_at = failure_time
        session.commit()
        raise

    submitted_at = datetime.now(timezone.utc)
    release.status = "provisioning"
    release.started_at = submitted_at
    release.updated_at = submitted_at
    release.provider_context = {**prepared_release.provider_context, **provider_submission}
    if payload.release_kind == "production":
        project_app.status = "deploying"
        project_app.updated_at = submitted_at
    session.commit()
    session.refresh(release)
    return _project_deployment_release_to_schema_with_source(session=session, tenant=tenant, release=release)


def update_project_deployment_release_status(
    *,
    session,
    tenant_id: str,
    project_id: str,
    release_id: str,
    payload: ProjectDeploymentReleaseStatusUpdate,
    app_id: str | None = None,
) -> ProjectDeploymentReleaseRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project_app = _resolve_project_app_scope(
        session=session,
        tenant_id=tenant_id,
        project=project,
        app_id=app_id,
    )
    release = session.get(ProjectDeploymentRelease, release_id)
    if release is None or release.tenant_id != tenant_id or release.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    release_app_id = _normalize_optional_string(release.app_id)
    if project_app.source_path == ".":
        if release_app_id is not None and release_app_id != project_app.app_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    elif release_app_id != project_app.app_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")

    current_status = str(release.status or "").strip()
    next_status = str(payload.status or "").strip()
    if current_status != next_status and next_status not in _RELEASE_ALLOWED_TRANSITIONS.get(current_status, set()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deployment release status cannot transition from '{current_status}' to '{next_status}'",
        )

    now = datetime.now(timezone.utc)
    release.status = next_status
    release.updated_at = now
    release.last_error = payload.last_error
    provider_context = _coerce_dict(release.provider_context)
    if payload.deployment_uuid is not None:
        provider_context["deployment_uuid"] = payload.deployment_uuid
        release.provider_context = provider_context
    if next_status in {"provisioning", "deploying"} and release.started_at is None:
        release.started_at = now
    if next_status in {"live", "failed", "rolled_back"}:
        release.completed_at = now
    elif next_status == "destroyed":
        release.destroyed_at = now
        release.completed_at = release.completed_at or now
    elif current_status != next_status:
        release.completed_at = None

    project_app = _resolve_project_app_scope(session=session, tenant_id=tenant_id, project=project, app_id=release.app_id)
    if str(release.release_kind or "").strip() == "production":
        if next_status in {"provisioning", "deploying", "route_activating"}:
            project_app.status = "deploying"
        elif next_status == "live":
            project_app.status = "live"
        elif next_status == "failed":
            project_app.status = "failed"
        elif next_status == "rolled_back":
            project_app.status = "ready"
        project_app.updated_at = now

    session.commit()
    session.refresh(release)
    return _project_deployment_release_to_schema_with_source(session=session, tenant=tenant, release=release)


def destroy_project_deployment_preview_release(
    *,
    session,
    tenant_id: str,
    project_id: str,
    release_id: str,
    reason: str,
) -> ProjectDeploymentReleaseRead:  # noqa: ANN001
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    release = session.get(ProjectDeploymentRelease, release_id)
    if release is None or release.tenant_id != tenant_id or release.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment release not found")
    if str(release.release_kind or "").strip() != "run_preview":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only run preview releases can be destroyed here")
    if str(release.status or "").strip() == "destroyed":
        return _project_deployment_release_to_schema_with_source(session=session, tenant=tenant, release=release)

    provider_context = _coerce_dict(release.provider_context)
    application_uuid = _normalize_optional_string(provider_context.get("application_uuid"))
    should_delete_application = _should_delete_preview_application_on_destroy(reason=reason)
    if application_uuid is not None and should_delete_application:
        tenant_plane = tenant_deployment_plane_to_schema(tenant)
        settings = get_settings()
        api_token_ref = str((tenant_plane.secret_refs or {}).get("coolify_api_token") or "").strip()
        if not api_token_ref:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tenant deployment plane is missing coolify_api_token secret ref",
            )
        api_token = _resolve_secret_value(
            session=session,
            secret_ref=api_token_ref,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            encryption_key=settings.secrets_encryption_key,
        )
        if api_token is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Tenant deployment plane references missing Coolify API token",
            )
        client = CoolifyApiClient(CoolifyApiConfig(base_url=_coolify_api_base_url(tenant_plane=tenant_plane), bearer_token=api_token))
        try:
            client.delete_application(application_uuid=application_uuid)
        except CoolifyApiError as exc:
            if "404" not in str(exc):
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Coolify preview deletion failed: {exc}",
                ) from exc

    provider_context["destroy_reason"] = _normalize_optional_string(reason) or "preview_cleanup"
    if not should_delete_application and application_uuid is not None:
        provider_context["application_retained_for_replacement"] = True
    release.provider_context = provider_context
    updated = update_project_deployment_release_status(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        release_id=release_id,
        app_id=release.app_id,
        payload=ProjectDeploymentReleaseStatusUpdate(status="destroyed", last_error=None),
    )
    if should_delete_application:
        ensure_local_preview_route_cleanup_command(
            session=session,
            tenant=tenant,
            project=project,
            release=release,
        )
    return updated


def prepare_project_deployment_release(
    *,
    tenant: Tenant,
    project: Project,
    project_app: ProjectApp,
    tenant_plane: TenantDeploymentPlaneRead,
    project_deployment: ProjectDeploymentConfigRead,
    payload: ProjectDeploymentReleaseCreate,
) -> PreparedDeploymentRelease:
    if tenant_plane.provider != "internal_coolify":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is not configured for internal Coolify",
        )
    if tenant_plane.state not in _ACTIVE_DEPLOYMENT_PLANE_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane must be active before creating deployment releases",
        )
    missing_coolify_fields = [
        field_name
        for field_name, value in (
            ("infrastructure_provider", tenant_plane.infrastructure_provider),
            ("base_domain", tenant_plane.base_domain),
            ("platform_subdomain", tenant_plane.platform_subdomain),
            ("api_base_url", tenant_plane.api_base_url),
            ("coolify_project_uuid", tenant_plane.coolify_project_uuid),
            ("coolify_environment_name", tenant_plane.coolify_environment_name),
            ("coolify_server_uuid", tenant_plane.coolify_server_uuid),
            ("coolify_destination_uuid", tenant_plane.coolify_destination_uuid),
            ("coolify_github_app_uuid", tenant_plane.coolify_github_app_uuid),
            ("secret_refs.coolify_api_token", (tenant_plane.secret_refs or {}).get("coolify_api_token")),
        )
        if _normalize_optional_string(value) is None
    ]
    if missing_coolify_fields:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant deployment plane is missing Coolify configuration fields: {', '.join(missing_coolify_fields)}",
        )
    if not project_deployment.enabled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project deployment config is disabled",
        )
    if not project_deployment.environment_name:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project deployment config must define environment_name before deployment",
        )
    if not project_deployment.source_strategy:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project deployment config must define source_strategy before deployment",
        )

    normalized_git_ref = _normalize_git_branch(payload.git_ref)
    normalized_commit_sha = _normalize_optional_string(payload.commit_sha)
    normalized_reason = _normalize_optional_string(payload.reason)

    deployment_snapshot: dict[str, object] = {
        "tenant_id": tenant.tenant_id,
        "project_id": project.project_id,
        "app_id": project_app.app_id,
        "project_name": project.name,
        "release_kind": payload.release_kind,
        "github_repository": project.github_repository,
        "jira_project_key": project.jira_project_key,
        "environment_name": project_deployment.environment_name,
        "source_strategy": project_deployment.source_strategy,
        "environment": dict(project_deployment.environment or {}),
        "secret_refs": dict(project_deployment.secret_refs or {}),
        "domains": [domain.model_dump(exclude_none=True) for domain in project_deployment.domains],
        "services": [service.model_dump(exclude_none=True) for service in project_deployment.services],
        "resources": [resource.model_dump(exclude_none=True) for resource in project_deployment.resources],
        "volumes": [volume.model_dump(exclude_none=True) for volume in project_deployment.volumes],
        "backup_policies": [
            backup_policy.model_dump(exclude_none=True)
            for backup_policy in project_deployment.backup_policies
        ],
    }
    deployment_snapshot["git_ref"] = normalized_git_ref
    if normalized_commit_sha is not None:
        deployment_snapshot["commit_sha"] = normalized_commit_sha
    if normalized_reason is not None:
        deployment_snapshot["reason"] = normalized_reason
    if payload.source_run_id is not None:
        deployment_snapshot["source_run_id"] = payload.source_run_id
    if payload.pr_number is not None:
        deployment_snapshot["pr_number"] = payload.pr_number

    provider_context = {
        "provider": "internal_coolify",
        "release_kind": payload.release_kind,
        "infrastructure_provider": tenant_plane.infrastructure_provider,
        "region": tenant_plane.region,
        "base_domain": tenant_plane.base_domain,
        "platform_subdomain": tenant_plane.platform_subdomain,
        "capabilities": {
            "domains": len(project_deployment.domains),
            "services": len(project_deployment.services),
            "resources": len(project_deployment.resources),
            "volumes": len(project_deployment.volumes),
            "backup_policies": len(project_deployment.backup_policies),
        },
    }

    return PreparedDeploymentRelease(
        provider="internal_coolify",
        environment_name=project_deployment.environment_name,
        source_strategy=project_deployment.source_strategy,
        deployment_snapshot=deployment_snapshot,
        provider_context=provider_context,
    )


def _project_deployment_config_for_release(
    *,
    project_app: ProjectApp,
    deployment_config_override: dict[str, object] | None,
) -> ProjectDeploymentConfigRead:
    if deployment_config_override is None:
        return project_deployment_config_to_schema(project_app)
    merged_config = {**_coerce_dict(project_app.deployment_config), **deployment_config_override}
    return ProjectDeploymentConfigRead.model_validate(merged_config)


def submit_internal_coolify_release(
    *,
    session,
    tenant: Tenant,
    project: Project,
    project_app: ProjectApp,
    tenant_plane: TenantDeploymentPlaneRead,
    project_deployment: ProjectDeploymentConfigRead,
    payload: ProjectDeploymentReleaseCreate,
    existing_application_uuid: str | None,
    existing_service_uuid: str | None,
) -> dict[str, object]:  # noqa: ANN001
    settings = get_settings()
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()

    api_token_ref = str((tenant_plane.secret_refs or {}).get("coolify_api_token") or "").strip()
    if not api_token_ref:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is missing coolify_api_token secret ref",
        )
    api_token = _resolve_secret_value(
        session=session,
        secret_ref=api_token_ref,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        encryption_key=encryption_key,
    )
    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane references missing Coolify API token",
        )

    api_base_url = _coolify_api_base_url(tenant_plane=tenant_plane)
    project_uuid = _required_plane_value(tenant_plane.coolify_project_uuid, "coolify_project_uuid")
    environment_name = _required_plane_value(
        tenant_plane.coolify_environment_name or project_deployment.environment_name,
        "coolify_environment_name",
    )
    server_uuid = _required_plane_value(tenant_plane.coolify_server_uuid, "coolify_server_uuid")
    destination_uuid = _required_plane_value(tenant_plane.coolify_destination_uuid, "coolify_destination_uuid")
    github_app_uuid = _required_plane_value(tenant_plane.coolify_github_app_uuid, "coolify_github_app_uuid")

    git_branch = _normalize_git_branch(payload.git_ref)
    coolify_git_repository = _coolify_github_repository(project.github_repository)
    env_payload = _build_coolify_environment_payload(
        session=session,
        project=project,
        project_deployment=project_deployment,
        git_ref=git_branch,
        encryption_key=encryption_key,
    )
    name = _coolify_application_name(project_app=project_app, payload=payload)
    source_path = _normalize_source_path(project_app.source_path)
    compose_location = _compose_location_for_source(source_path)
    base_directory = _coolify_base_directory(source_path)
    docker_compose_raw = None
    docker_compose_routes: dict[str, str] = {}
    docker_compose_ports: dict[str, str] = {}
    route_bindings = []
    if project_deployment.source_strategy == "docker_compose":
        generated_compose_raw = _normalize_optional_string(project_deployment.generated_compose_raw)
        compose_result = _load_normalized_compose_for_release(
            session=session,
            tenant=tenant,
            project=project,
            source_path=source_path,
            compose_location=compose_location,
            git_ref=git_branch,
            encryption_key=encryption_key,
            generated_compose_raw=generated_compose_raw,
        )
        docker_compose_routes, docker_compose_ports, route_bindings = _coolify_docker_compose_routes(
            tenant=tenant,
            project=project,
            project_app=project_app,
            tenant_plane=tenant_plane,
            project_deployment=project_deployment,
            git_ref=git_branch,
            release_kind=payload.release_kind,
            exposed_ports_by_service=compose_result.exposed_ports_by_service,
        )
        docker_compose_raw = compose_result.compose_raw

    client = CoolifyApiClient(CoolifyApiConfig(base_url=api_base_url, bearer_token=api_token))
    existing_application_uuid = _resolve_existing_coolify_application_uuid(
        client=client,
        application_uuid=existing_application_uuid,
    )
    try:
        if project_deployment.source_strategy == "docker_compose":
            if docker_compose_raw is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Docker Compose release preparation did not produce compose content",
                )
            compose_location = _normalize_optional_string(project_deployment.deployment_compose_path)
            if compose_location is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Docker Compose deployment requires a committed deployment_compose_path",
                )
            application_payload = {
                "project_uuid": project_uuid,
                "environment_name": environment_name,
                "server_uuid": server_uuid,
                "destination_uuid": destination_uuid,
                "github_app_uuid": github_app_uuid,
                "name": name,
                "description": f"Managed by Master Builder for tenant {tenant.tenant_id}, project {project.project_id}",
                "git_repository": coolify_git_repository,
                "git_branch": git_branch,
                "git_commit_sha": _normalize_optional_string(payload.commit_sha),
                "build_pack": "dockercompose",
                "ports_exposes": "80",
                "base_directory": "/",
                "docker_compose_location": f"/{compose_location.lstrip('/')}",
                "docker_compose_domains": [
                    {"name": service_name, "domain": domain}
                    for service_name, domain in sorted(docker_compose_routes.items())
                ],
                "instant_deploy": False,
                "force_domain_override": True,
                "connect_to_docker_network": True,
                "is_auto_deploy_enabled": True,
            }
            if existing_application_uuid:
                application_uuid = existing_application_uuid
                client.update_application(
                    application_uuid=application_uuid,
                    payload=_coolify_application_update_payload(application_payload),
                )
            else:
                application_uuid = client.create_private_github_app_application(payload=application_payload)
            if env_payload:
                client.bulk_update_application_envs(
                    application_uuid=application_uuid,
                    payload={"data": env_payload},
                )
            deployment_uuid = client.start_application(application_uuid=application_uuid)
            return {
                "api_base_url": api_base_url,
                "application_uuid": application_uuid,
                "deployment_uuid": deployment_uuid,
                "git_branch": git_branch,
                "environment_name": environment_name,
                "route_bindings": route_bindings,
                "coolify_resource_type": "application",
            }

        application_payload = {
            "project_uuid": project_uuid,
            "environment_name": environment_name,
            "server_uuid": server_uuid,
            "destination_uuid": destination_uuid,
            "github_app_uuid": github_app_uuid,
            "name": name,
            "description": f"Managed by Master Builder for tenant {tenant.tenant_id}, project {project.project_id}",
            "git_repository": coolify_git_repository,
            "git_branch": git_branch,
            "build_pack": _coolify_build_pack(project_deployment.source_strategy),
            "ports_exposes": _coolify_ports_exposes(project_app=project_app),
            "autogenerate_domain": False,
            "base_directory": base_directory,
        }
        start_command = _coolify_start_command(project_app=project_app, project_deployment=project_deployment)
        if start_command is not None:
            application_payload["start_command"] = start_command
        install_command = _coolify_install_command(project_app=project_app, project_deployment=project_deployment)
        if install_command is not None:
            application_payload["install_command"] = install_command
        build_command = _normalize_optional_string(getattr(project_deployment, "build_command", None))
        if build_command is not None:
            application_payload["build_command"] = build_command
        if project_deployment.source_strategy == "dockerfile":
            application_payload["dockerfile_location"] = "/Dockerfile"
        domains = "" if payload.release_kind == "run_preview" else ",".join(_domain_url(domain) for domain in project_deployment.domains)
        application_payload["domains"] = domains
        application_payload["autogenerate_domain"] = not bool(domains)
        commit_sha = _normalize_optional_string(payload.commit_sha)
        if commit_sha is not None:
            application_payload["git_commit_sha"] = commit_sha

        if existing_application_uuid:
            application_uuid = existing_application_uuid
            client.update_application(
                application_uuid=application_uuid,
                payload=_coolify_application_update_payload(application_payload),
            )
        else:
            application_uuid = client.create_private_github_app_application(payload=application_payload)

        if env_payload:
            client.bulk_update_application_envs(
                application_uuid=application_uuid,
                payload={"data": env_payload},
            )
        deployment_uuid = client.start_application(application_uuid=application_uuid)
        application = client.get_application(application_uuid=application_uuid)
    except CoolifyApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Coolify deployment submission failed: {exc}",
        ) from exc

    fqdn = _normalize_optional_string(application.get("fqdn"))
    if fqdn is not None and project_deployment.source_strategy != "docker_compose":
        route_binding = _route_binding_from_url(
            service_key=project_app.slug,
            service_name=project_app.name,
            service_kind="website",
            url=fqdn,
            url_kind="generated" if not project_deployment.domains else "custom",
            port=_coolify_ports_exposes(project_app=project_app),
        )
        route_bindings.append(route_binding)
    return {
        "api_base_url": api_base_url,
        "application_uuid": application_uuid,
        "deployment_uuid": deployment_uuid,
        "git_branch": git_branch,
        "environment_name": environment_name,
        "route_bindings": route_bindings,
    }


def _normalize_source_path(value: object) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized or normalized == ".":
        return "."
    return normalized.strip("/")


def _coolify_application_update_payload(payload: dict[str, object]) -> dict[str, object]:
    create_only_fields = {
        "project_uuid",
        "environment_name",
        "server_uuid",
        "destination_uuid",
        "github_app_uuid",
        "docker_compose_domains",
    }
    return {key: value for key, value in payload.items() if key not in create_only_fields}


def _resolve_existing_coolify_application_uuid(
    *,
    client: CoolifyApiClient,
    application_uuid: str | None,
) -> str | None:
    normalized_application_uuid = _normalize_optional_string(application_uuid)
    if normalized_application_uuid is None:
        return None
    try:
        client.get_application(application_uuid=normalized_application_uuid)
    except CoolifyApiError as exc:
        if _is_missing_coolify_resource_error(exc):
            return None
        raise
    return normalized_application_uuid


def _is_missing_coolify_resource_error(exc: CoolifyApiError) -> bool:
    message = str(exc).lower()
    return "request failed (404)" in message or "not found" in message


def _coolify_base_directory(source_path: str) -> str:
    return "/" if source_path == "." else f"/{source_path}"


def _compose_location_for_source(source_path: str) -> str:
    return "/docker-compose.yml"


def _load_normalized_compose_for_release(
    *,
    session,
    tenant: Tenant,
    project: Project,
    source_path: str,
    compose_location: str,
    git_ref: str,
    encryption_key: str,
    generated_compose_raw: str | None = None,
) -> CoolifyComposeNormalizationResult:  # noqa: ANN001
    if generated_compose_raw is not None:
        try:
            return normalize_generated_compose_for_coolify(generated_compose_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Generated Docker Compose file is not deployable by Coolify: {exc}",
            ) from exc
    repo_full_name = _github_repo_full_name(project.github_repository)
    compose_path = "/".join(
        part.strip("/")
        for part in (None if source_path == "." else source_path, compose_location)
        if part and str(part).strip("/")
    )
    try:
        github_client = github_client_from_tenant_config(
            tenant.github_config,
            tenant_secret_lookup=lambda secret_ref: _resolve_secret_value(
                session=session,
                secret_ref=secret_ref,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                encryption_key=encryption_key,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=encryption_key,
            ),
        )
        compose_raw = github_client.get_file_text_at_ref(
            repo_full_name=repo_full_name,
            path=compose_path,
            ref=git_ref,
        )
    except (GitHubApiError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to load Docker Compose file from GitHub: {exc}",
        ) from exc
    try:
        return normalize_compose_for_coolify(compose_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Docker Compose file is not deployable by Coolify: {exc}",
        ) from exc


def _coolify_docker_compose_routes(
    *,
    tenant: Tenant,
    project: Project,
    project_app: ProjectApp,
    tenant_plane: TenantDeploymentPlaneRead,
    project_deployment: ProjectDeploymentConfigRead,
    git_ref: str,
    exposed_ports_by_service: dict[str, list[str]],
    release_kind: str = "production",
) -> tuple[dict[str, str], dict[str, str], list[dict[str, object]]]:
    routes: dict[str, str] = {}
    ports: dict[str, str] = {}
    route_bindings: list[dict[str, object]] = []
    if release_kind != "run_preview" and project_deployment.domains:
        for domain in project_deployment.domains:
            service_key = str(domain.service_key or "").strip()
            url = _domain_url(domain)
            routes[service_key] = url
            ports[service_key] = _service_container_port(
                service_key=service_key,
                resource_config={},
                exposed_ports_by_service=exposed_ports_by_service,
            )
            route_bindings.append(_route_binding_from_url(
                service_key=domain.service_key,
                service_name=domain.service_key,
                service_kind="website",
                url=url,
                url_kind="custom",
                domain_key=_normalize_optional_string(domain.key),
            ))
        return routes, ports, route_bindings

    base_domain = _required_plane_value(tenant_plane.base_domain, "base_domain")
    for service in project_deployment.services:
        config = dict(service.config or {})
        if service.public is not True:
            continue
        service_key = str(service.compose_service or service.key).strip()
        if not service_key:
            continue
        scheme = _generated_route_scheme(base_domain=base_domain)
        host = _generated_service_host(
            project=project,
            project_app=project_app,
            service_key=service_key,
            base_domain=base_domain,
            git_ref=git_ref,
            release_kind=release_kind,
        )
        path = ""
        routes[service_key] = f"{scheme}://{host}"
        internal_url = _local_base_domain_probe_url(base_domain)
        ports[service_key] = _service_container_port(
            service_key=service_key,
            resource_config={
                **config,
                "container_port": service.container_port,
            },
            exposed_ports_by_service=exposed_ports_by_service,
        )
        route_bindings.append(
            {
                "service_key": service_key,
                "service_name": service.name or service_key,
                "service_kind": service.kind,
                "scheme": scheme,
                "host": host,
                "path": path,
                "url_kind": "generated",
                "status": "pending",
                "port": ports[service_key],
                "proxy_port": _base_domain_port(base_domain),
                "internal_url": internal_url,
            }
        )
    return routes, ports, route_bindings


def _service_container_port(
    *,
    service_key: str,
    resource_config: dict[str, object],
    exposed_ports_by_service: dict[str, list[str]],
) -> str:
    for key in ("container_port", "target_port", "exposed_port", "port"):
        configured = _normalize_optional_string(resource_config.get(key))
        if configured is not None:
            return configured
    configured_ports = resource_config.get("ports")
    if isinstance(configured_ports, list):
        for item in configured_ports:
            if not isinstance(item, dict):
                continue
            configured = _normalize_optional_string(item.get("container_port") or item.get("target"))
            if configured is not None:
                return configured
    exposed_ports = exposed_ports_by_service.get(service_key) or []
    if exposed_ports:
        return exposed_ports[0]
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Deployment service '{service_key}' needs a container_port in deployment resource config",
    )


def _domain_url(domain) -> str:  # noqa: ANN001
    host = str(domain.host or "").strip()
    if host.startswith(("http://", "https://")):
        base = host
    else:
        scheme = "https" if domain.tls_enabled else "http"
        base = f"{scheme}://{host}"
    path = str(domain.path or "").strip()
    return f"{base}{path}" if path else base


def _route_binding_from_url(
    *,
    service_key: str,
    service_name: str,
    service_kind: str,
    url: str,
    url_kind: str,
    domain_key: str | None = None,
    port: str | int | None = None,
) -> dict[str, object]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Deployment route for service '{service_key}' must be an absolute http(s) URL",
        )
    proxy_port = parsed.port if parsed.port is not None else port
    return {
        "service_key": service_key,
        "service_name": service_name,
        "service_kind": "api" if service_kind == "api" else "website",
        "scheme": parsed.scheme,
        "host": parsed.hostname.lower(),
        "path": parsed.path or "",
        "url_kind": "custom" if url_kind == "custom" else "generated",
        "status": "pending",
        "port": str(proxy_port) if proxy_port is not None else None,
        "proxy_port": proxy_port,
        "domain_key": domain_key,
    }


def _generated_service_host(
    *,
    project: Project,
    project_app: ProjectApp,
    service_key: str,
    base_domain: str,
    git_ref: str,
    release_kind: str = "production",
) -> str:
    normalized_base_domain = _base_domain_hostname(base_domain)
    if release_kind == "run_preview":
        return ".".join(
            [
                _preview_dns_label(service_key, project_app.slug or project_app.name, git_ref),
                _dns_label(project.name or project.project_id),
                normalized_base_domain,
            ]
        )
    host = ".".join(
        [
            _dns_label(service_key),
            _dns_label(project_app.slug or project_app.name),
            _dns_label(git_ref),
            _dns_label(project.name or project.project_id),
            normalized_base_domain,
        ]
    )
    return host


def _base_domain_hostname(value: str) -> str:
    normalized = value.strip().lower().rstrip(".")
    parsed = urlsplit(normalized if "://" in normalized else f"//{normalized}")
    if parsed.hostname is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane base_domain must be a hostname, not a URL or host:port endpoint",
        )
    return parsed.hostname


def _base_domain_port(value: str) -> int | None:
    normalized = value.strip().lower().rstrip(".")
    parsed = urlsplit(normalized if "://" in normalized else f"//{normalized}")
    return parsed.port


def _generated_route_scheme(*, base_domain: str) -> str:
    normalized = base_domain.strip().lower().rstrip(".")
    parsed = urlsplit(normalized if "://" in normalized else f"//{normalized}")
    if parsed.scheme in {"http", "https"}:
        return parsed.scheme
    hostname = parsed.hostname or ""
    port = parsed.port
    if hostname == "localhost" or hostname.endswith(".localhost"):
        return "http"
    if port is not None and port not in {443, 8443}:
        return "http"
    return "https"


def deployment_release_routes_match_base_domain(
    *,
    provider_context: dict[str, object],
    base_domain: str,
    require_preview_wildcard_shape: bool = False,
) -> bool:
    route_bindings = provider_context.get("route_bindings")
    if not isinstance(route_bindings, list) or not route_bindings:
        return True
    expected_scheme = _generated_route_scheme(base_domain=base_domain)
    expected_internal_url = _local_base_domain_probe_url(base_domain)
    for route_binding in route_bindings:
        if not isinstance(route_binding, dict):
            return False
        if _normalize_optional_string(route_binding.get("scheme")) != expected_scheme:
            return False
        if expected_internal_url is not None and _normalize_optional_string(route_binding.get("internal_url")) != expected_internal_url:
            return False
        if require_preview_wildcard_shape and not _host_matches_preview_wildcard_shape(
            host=_normalize_optional_string(route_binding.get("host")),
            base_domain=base_domain,
        ):
            return False
    return True


def _run_preview_release_can_reuse_existing_application(
    *,
    provider_context: dict[str, object],
    tenant_plane: TenantDeploymentPlaneRead,
) -> bool:
    existing_base_domain = _normalize_optional_string(provider_context.get("base_domain"))
    current_base_domain = _normalize_optional_string(tenant_plane.base_domain)
    if existing_base_domain is None or current_base_domain is None:
        return False
    if existing_base_domain != current_base_domain:
        return False
    return deployment_release_routes_match_base_domain(
        provider_context=provider_context,
        base_domain=current_base_domain,
        require_preview_wildcard_shape=True,
    )


def _should_delete_preview_application_on_destroy(*, reason: str) -> bool:
    return _normalize_optional_string(reason) != "preview_replaced"


def _local_base_domain_probe_url(value: str) -> str | None:
    normalized = value.strip().lower().rstrip(".")
    parsed = urlsplit(normalized if "://" in normalized else f"//{normalized}")
    hostname = parsed.hostname
    if hostname is None:
        return None
    port = parsed.port
    is_localhost_domain = hostname == "localhost" or hostname.endswith(".localhost")
    is_local_coolify_proxy = port in {8088, 8443}
    if not is_localhost_domain and not is_local_coolify_proxy:
        return None
    netloc = f"host.docker.internal:{port}" if port is not None else "host.docker.internal"
    scheme = _generated_route_scheme(base_domain=value)
    return f"{scheme}://{netloc}"


def _dns_label(value: object) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else "-"
        for character in str(value or "").strip()
    ).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized or "service"


def _preview_dns_label(*parts: object) -> str:
    raw = "-".join(str(part or "").strip() for part in parts if str(part or "").strip())
    label = _dns_label(raw)
    if len(label) <= 63:
        return label
    digest = hashlib.sha1(label.encode("utf-8")).hexdigest()[:10]
    return f"{label[:52].rstrip('-')}-{digest}"


def _host_matches_preview_wildcard_shape(*, host: str | None, base_domain: str) -> bool:
    normalized_host = _normalize_optional_string(host)
    if normalized_host is None:
        return False
    base_hostname = _base_domain_hostname(base_domain)
    suffix = f".{base_hostname}"
    if not normalized_host.endswith(suffix):
        return False
    prefix = normalized_host[: -len(suffix)].strip(".")
    return len([part for part in prefix.split(".") if part]) == 2


def _github_repo_full_name(github_repository: str | None) -> str:
    normalized = str(github_repository or "").strip()
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project must define github_repository before deployment",
        )
    if "://" in normalized:
        parsed = urlparse(normalized)
        path = parsed.path.strip("/")
    else:
        path = normalized.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project github_repository must resolve to owner/repo",
        )
    return f"{parts[0]}/{parts[1]}"


def _coolify_build_pack(source_strategy: str | None) -> str:
    if source_strategy == "docker_compose":
        return "dockercompose"
    if source_strategy == "dockerfile":
        return "dockerfile"
    if source_strategy == "nixpacks":
        return "nixpacks"
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Project deployment config source_strategy must be dockerfile, docker_compose, or nixpacks",
    )


def _coolify_ports_exposes(*, project_app: ProjectApp) -> str:
    if project_app.exposed_port is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project app must define exposed_port before Coolify deployment",
        )
    return str(int(project_app.exposed_port))


def _uses_stale_legacy_expo_contract(*, project_app: ProjectApp, project_deployment: ProjectDeploymentConfigRead) -> bool:
    return (
        _normalize_optional_string(project_app.detected_runtime) == "react_native_web"
        and _normalize_optional_string(project_app.start_command) == "npm run web"
        and _normalize_optional_string(project_deployment.install_command) == NODE_INSTALL_WITH_LEGACY_PEERS_COMMAND
    )


def _coolify_start_command(*, project_app: ProjectApp, project_deployment: ProjectDeploymentConfigRead) -> str | None:
    if _uses_stale_legacy_expo_contract(project_app=project_app, project_deployment=project_deployment):
        return LEGACY_EXPO_WEB_START_COMMAND
    return _normalize_optional_string(project_app.start_command)


def _coolify_install_command(*, project_app: ProjectApp, project_deployment: ProjectDeploymentConfigRead) -> str | None:
    if _uses_stale_legacy_expo_contract(project_app=project_app, project_deployment=project_deployment):
        return LEGACY_EXPO_CLI_INSTALL_COMMAND
    return _normalize_optional_string(project_deployment.install_command)


def _coolify_application_name(*, project_app: ProjectApp, payload: ProjectDeploymentReleaseCreate) -> str:
    base_name = _normalize_coolify_name(project_app.name, fallback=project_app.app_id)
    if payload.release_kind != "run_preview":
        return base_name
    source = payload.source_run_id or payload.pr_number or payload.commit_sha
    suffix = _normalize_coolify_name(str(source), fallback=payload.commit_sha[:12])
    return f"{base_name}-preview-{suffix}"[:63].strip("-")


def _required_plane_value(value: str | None, field_name: str) -> str:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Tenant deployment plane is missing required field {field_name}",
        )
    return normalized


def _coolify_api_base_url(*, tenant_plane: TenantDeploymentPlaneRead) -> str:
    configured = _normalize_optional_string(tenant_plane.api_base_url)
    if configured is not None:
        return _normalize_local_coolify_api_base_url(configured.rstrip("/"))
    if tenant_plane.platform_subdomain and tenant_plane.base_domain:
        return f"https://{tenant_plane.platform_subdomain}.{tenant_plane.base_domain}/api/v1"
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Tenant deployment plane is missing api_base_url or platform domain metadata",
    )


def _normalize_local_coolify_api_base_url(configured: str) -> str:
    parsed = urlsplit(configured)
    if parsed.hostname != "host.docker.internal" or Path("/.dockerenv").exists():
        return configured
    netloc = "localhost"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _normalize_git_branch(git_ref: str | None) -> str:
    normalized = _normalize_optional_string(git_ref)
    if normalized is None:
        raise ValueError("Deployment release requires git_ref")
    prefixes = ("refs/heads/", "origin/")
    for prefix in prefixes:
        if normalized.startswith(prefix):
            branch = normalized[len(prefix):].strip()
            if not branch:
                raise ValueError("Deployment release requires git_ref")
            return branch
    return normalized


def _coolify_github_repository(github_repository: str | None) -> str:
    normalized = _normalize_optional_string(github_repository)
    if normalized is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Project is missing github_repository required by Coolify GitHub App deployments",
        )
    if normalized.startswith("git@") and ":" in normalized:
        normalized = normalized.split(":", 1)[1]
    elif "://" in normalized:
        parsed = urlsplit(normalized)
        normalized = parsed.path.lstrip("/")
    normalized = normalized.removesuffix(".git").strip("/")
    parts = [part for part in normalized.split("/") if part]
    if len(parts) != 2:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Coolify GitHub App deployments require a repository in owner/repo form",
        )
    return "/".join(parts)


def _normalize_coolify_name(name: str, *, fallback: str) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else "-"
        for character in str(name or "").strip()
    ).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized or fallback


def _resolve_secret_value(
    *,
    session,
    secret_ref: str,
    tenant_id: str,
    project_id: str | None,
    encryption_key: str,
) -> str | None:  # noqa: ANN001
    normalized_ref = normalize_secret_ref(secret_ref)
    if normalized_ref.startswith("platform/"):
        return resolve_platform_secret_ref(
            session,
            secret_ref=normalized_ref,
            encryption_key=encryption_key,
        )
    return resolve_scoped_secret_ref(
        session,
        secret_ref=normalized_ref,
        encryption_key=encryption_key,
        tenant_id=tenant_id,
        project_id=project_id,
    )


def _coolify_client_for_release(
    *,
    session,
    tenant: Tenant,
    project: Project,
    tenant_plane: TenantDeploymentPlaneRead,
) -> CoolifyApiClient:  # noqa: ANN001
    settings = get_settings()
    api_token_ref = str((tenant_plane.secret_refs or {}).get("coolify_api_token") or "").strip()
    if not api_token_ref:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is missing coolify_api_token secret ref",
        )
    api_token = _resolve_secret_value(
        session=session,
        secret_ref=api_token_ref,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        encryption_key=settings.secrets_encryption_key,
    )
    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane references missing Coolify API token",
        )
    return CoolifyApiClient(CoolifyApiConfig(base_url=_coolify_api_base_url(tenant_plane=tenant_plane), bearer_token=api_token))


def _coolify_deployment_payload_for_logs(
    *,
    client: CoolifyApiClient,
    deployment_uuid: str | None,
    application_uuid: str | None,
    service_uuid: str | None,
) -> dict[str, object]:
    if deployment_uuid is not None:
        try:
            return client.get_deployment(deployment_uuid=deployment_uuid)
        except CoolifyApiError as exc:
            if exc.status_code != status.HTTP_404_NOT_FOUND or application_uuid is None:
                raise
    if application_uuid is not None:
        deployments = client.list_application_deployments(application_uuid=application_uuid, take=1)
        if deployments:
            return deployments[0]
    if service_uuid is not None:
        return client.get_service(service_uuid=service_uuid)
    return {}


def _logs_from_provider_payload(payload: dict[str, object]) -> str:
    for key in ("logs", "log", "output", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            lines = [
                item if isinstance(item, str) else str(item)
                for item in value
                if isinstance(item, str) or item is not None
            ]
            if lines:
                return "\n".join(lines)
    return ""


def _bounded_provider_logs(logs: str) -> tuple[str, bool]:
    if len(logs) <= _MAX_PROVIDER_LOG_CHARS:
        return logs, False
    return logs[-_MAX_PROVIDER_LOG_CHARS:], True


def _policy_branch_settings(*, project: Project, git_ref: str) -> tuple[dict[str, str], dict[str, str]]:
    deployment_policy = ProjectDeploymentPolicyRead.model_validate(dict(project.deployment_config or {}))
    branch = _normalize_git_branch(git_ref)
    settings = deployment_policy.branch_settings.get(branch)
    if settings is None:
        return {}, {}
    return dict(settings.environment), dict(settings.secret_refs)


def _merged_environment_literals(
    *,
    project: Project,
    project_deployment: ProjectDeploymentConfigRead,
    git_ref: str,
) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in sorted((project.environment or {}).items()):
        normalized_key = _normalize_optional_string(key)
        normalized_value = _normalize_optional_string(value)
        if normalized_key is None or normalized_value is None:
            continue
        environment[normalized_key] = normalized_value
    policy_environment, _policy_secret_refs = _policy_branch_settings(project=project, git_ref=git_ref)
    for key, value in sorted(policy_environment.items()):
        normalized_key = _normalize_optional_string(key)
        normalized_value = _normalize_optional_string(value)
        if normalized_key is None or normalized_value is None:
            continue
        environment[normalized_key] = normalized_value
    for key, value in sorted((project_deployment.environment or {}).items()):
        normalized_key = _normalize_optional_string(key)
        normalized_value = _normalize_optional_string(value)
        if normalized_key is None or normalized_value is None:
            continue
        environment[normalized_key] = normalized_value
    return environment


def _merged_secret_refs(
    *,
    project: Project,
    project_deployment: ProjectDeploymentConfigRead,
    git_ref: str,
) -> dict[str, str]:
    secret_refs: dict[str, str] = {}
    for key, secret_ref in sorted((project.secret_refs or {}).items()):
        normalized_key = _normalize_optional_string(key)
        normalized_secret_ref = _normalize_optional_string(secret_ref)
        if normalized_key is None or normalized_secret_ref is None:
            continue
        secret_refs[normalized_key] = normalized_secret_ref
    _policy_environment, policy_secret_refs = _policy_branch_settings(project=project, git_ref=git_ref)
    for key, secret_ref in sorted(policy_secret_refs.items()):
        normalized_key = _normalize_optional_string(key)
        normalized_secret_ref = _normalize_optional_string(secret_ref)
        if normalized_key is None or normalized_secret_ref is None:
            continue
        secret_refs[normalized_key] = normalized_secret_ref
    for key, secret_ref in sorted((project_deployment.secret_refs or {}).items()):
        normalized_key = _normalize_optional_string(key)
        normalized_secret_ref = _normalize_optional_string(secret_ref)
        if normalized_key is None or normalized_secret_ref is None:
            continue
        secret_refs[normalized_key] = normalized_secret_ref
    return secret_refs


def build_deployment_environment_values(
    *,
    session,
    project: Project,
    project_deployment: ProjectDeploymentConfigRead,
    git_ref: str,
    encryption_key: str,
) -> dict[str, str]:  # noqa: ANN001
    environment = _merged_environment_literals(project=project, project_deployment=project_deployment, git_ref=git_ref)
    for key, secret_ref in sorted(_merged_secret_refs(project=project, project_deployment=project_deployment, git_ref=git_ref).items()):
        resolved_secret = _resolve_secret_value(
            session=session,
            secret_ref=secret_ref,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            encryption_key=encryption_key,
        )
        if resolved_secret is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Deployment secret ref for {key} could not be resolved",
            )
        environment[key] = resolved_secret
    return environment


def _build_coolify_environment_payload(
    *,
    session,
    project: Project,
    project_deployment: ProjectDeploymentConfigRead,
    git_ref: str,
    encryption_key: str,
) -> list[dict[str, object]]:  # noqa: ANN001
    payload: list[dict[str, object]] = []
    literal_keys = set(_merged_environment_literals(project=project, project_deployment=project_deployment, git_ref=git_ref))
    resolved_environment = build_deployment_environment_values(
        session=session,
        project=project,
        project_deployment=project_deployment,
        git_ref=git_ref,
        encryption_key=encryption_key,
    )
    for key, value in sorted(resolved_environment.items()):
        payload.append(
            {
                "key": key,
                "value": value,
                "is_literal": key in literal_keys,
                "is_preview": False,
            }
        )
    return payload
