from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.core.config import get_settings
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
    ProjectDeploymentReleaseRead,
    ProjectDeploymentReleaseStatusUpdate,
    TenantDeploymentPlaneRead,
)
from orchestrator.api.deployment_schemas import redact_deployment_config_secrets
from orchestrator.storage.models import Project, ProjectApp, ProjectDeploymentRelease, Tenant
from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig, CoolifyApiError

_ACTIVE_DEPLOYMENT_PLANE_STATES = {"active", "degraded"}
_QUEUED_RELEASE_STATUS = "queued"
_RELEASE_ALLOWED_TRANSITIONS = {
    "queued": {"provisioning", "failed"},
    "provisioning": {"deploying", "failed"},
    "deploying": {"live", "failed"},
    "live": {"rolled_back"},
    "failed": set(),
    "rolled_back": set(),
}


@dataclass(frozen=True)
class PreparedDeploymentRelease:
    provider: str
    environment_name: str | None
    source_strategy: str | None
    deployment_snapshot: dict[str, object]
    provider_context: dict[str, object]


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


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
    if project_app.source_path == ".":
        return ((ProjectDeploymentRelease.app_id == project_app.app_id) | ProjectDeploymentRelease.app_id.is_(None),)
    return (ProjectDeploymentRelease.app_id == project_app.app_id,)


def project_deployment_release_to_schema(release: ProjectDeploymentRelease) -> ProjectDeploymentReleaseRead:
    return ProjectDeploymentReleaseRead(
        release_id=release.release_id,
        tenant_id=release.tenant_id,
        project_id=release.project_id,
        app_id=release.app_id,
        provider=release.provider,
        status=release.status,
        environment_name=release.environment_name,
        source_strategy=release.source_strategy,
        git_ref=release.git_ref,
        commit_sha=release.commit_sha,
        requested_by_user_id=release.requested_by_user_id,
        deployment_snapshot=redact_deployment_config_secrets(_coerce_dict(release.deployment_snapshot)),
        provider_context=_coerce_dict(release.provider_context),
        last_error=release.last_error,
        requested_at=release.requested_at,
        started_at=release.started_at,
        completed_at=release.completed_at,
        created_at=release.created_at,
        updated_at=release.updated_at,
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
    return [project_deployment_release_to_schema(release) for release in releases]


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
    return project_deployment_release_to_schema(release)


def create_project_deployment_release(
    *,
    session,
    tenant_id: str,
    project_id: str,
    payload: ProjectDeploymentReleaseCreate,
    requested_by_user_id: str | None,
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
    project_deployment = project_deployment_config_to_schema(project_app)
    prepared_release = prepare_project_deployment_release(
        tenant=tenant,
        project=project,
        project_app=project_app,
        tenant_plane=tenant_plane,
        project_deployment=project_deployment,
        payload=payload,
    )
    latest_release = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            *_release_scope_filter(project_app=project_app),
            ProjectDeploymentRelease.provider == prepared_release.provider,
        )
        .order_by(ProjectDeploymentRelease.created_at.desc())
    ).scalars().first()
    existing_application_uuid = None
    if latest_release is not None:
        existing_application_uuid = str(_coerce_dict(latest_release.provider_context).get("application_uuid") or "").strip() or None

    now = datetime.now(timezone.utc)
    release = ProjectDeploymentRelease(
        release_id=str(uuid4()),
        tenant_id=tenant_id,
        project_id=project_id,
        app_id=project_app.app_id,
        provider=prepared_release.provider,
        status=_QUEUED_RELEASE_STATUS,
        environment_name=prepared_release.environment_name,
        source_strategy=prepared_release.source_strategy,
        git_ref=_normalize_optional_string(payload.git_ref),
        commit_sha=_normalize_optional_string(payload.commit_sha),
        requested_by_user_id=_normalize_optional_string(requested_by_user_id),
        deployment_snapshot=prepared_release.deployment_snapshot,
        provider_context=prepared_release.provider_context,
        last_error=None,
        requested_at=now,
        started_at=None,
        completed_at=None,
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
            tenant_plane=tenant_plane,
            project_deployment=project_deployment,
            payload=payload,
            existing_application_uuid=existing_application_uuid,
        )
    except HTTPException as exc:
        failure_time = datetime.now(timezone.utc)
        release.status = "failed"
        release.last_error = str(exc.detail)
        release.completed_at = failure_time
        release.updated_at = failure_time
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
        project_app.status = "failed"
        project_app.updated_at = failure_time
        session.commit()
        raise

    submitted_at = datetime.now(timezone.utc)
    release.status = "provisioning"
    release.started_at = submitted_at
    release.updated_at = submitted_at
    release.provider_context = {**prepared_release.provider_context, **provider_submission}
    project_app.status = "deploying"
    project_app.updated_at = submitted_at
    session.commit()
    session.refresh(release)
    return project_deployment_release_to_schema(release)


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
    elif current_status != next_status:
        release.completed_at = None

    project_app = _resolve_project_app_scope(session=session, tenant_id=tenant_id, project=project, app_id=release.app_id)
    if next_status in {"provisioning", "deploying"}:
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
    return project_deployment_release_to_schema(release)


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
    if not tenant_plane.infrastructure_provider or not tenant_plane.base_domain or not tenant_plane.platform_subdomain:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is missing required infrastructure metadata",
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

    normalized_git_ref = _normalize_optional_string(payload.git_ref)
    normalized_commit_sha = _normalize_optional_string(payload.commit_sha)
    normalized_reason = _normalize_optional_string(payload.reason)

    deployment_snapshot: dict[str, object] = {
        "tenant_id": tenant.tenant_id,
        "project_id": project.project_id,
        "app_id": project_app.app_id,
        "project_name": project.name,
        "github_repository": project.github_repository,
        "jira_project_key": project.jira_project_key,
        "environment_name": project_deployment.environment_name,
        "source_strategy": project_deployment.source_strategy,
        "domains": [domain.model_dump(exclude_none=True) for domain in project_deployment.domains],
        "resources": [resource.model_dump(exclude_none=True) for resource in project_deployment.resources],
        "backup_policies": [
            backup_policy.model_dump(exclude_none=True)
            for backup_policy in project_deployment.backup_policies
        ],
    }
    if normalized_git_ref is not None:
        deployment_snapshot["git_ref"] = normalized_git_ref
    if normalized_commit_sha is not None:
        deployment_snapshot["commit_sha"] = normalized_commit_sha
    if normalized_reason is not None:
        deployment_snapshot["reason"] = normalized_reason

    provider_context = {
        "provider": "internal_coolify",
        "infrastructure_provider": tenant_plane.infrastructure_provider,
        "region": tenant_plane.region,
        "base_domain": tenant_plane.base_domain,
        "platform_subdomain": tenant_plane.platform_subdomain,
        "capabilities": {
            "domains": len(project_deployment.domains),
            "resources": len(project_deployment.resources),
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


def submit_internal_coolify_release(
    *,
    session,
    tenant: Tenant,
    project: Project,
    tenant_plane: TenantDeploymentPlaneRead,
    project_deployment: ProjectDeploymentConfigRead,
    payload: ProjectDeploymentReleaseCreate,
    existing_application_uuid: str | None,
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

    git_branch = _normalize_git_branch(payload.git_ref)
    env_payload = _build_coolify_environment_payload(
        session=session,
        project=project,
        encryption_key=encryption_key,
    )
    domains = ",".join(domain.host for domain in project_deployment.domains if domain.host)
    name = _normalize_coolify_name(project.name, fallback=project.project_id)

    client = CoolifyApiClient(CoolifyApiConfig(base_url=api_base_url, bearer_token=api_token))
    try:
        if project_deployment.source_strategy != "dockerfile":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Internal Coolify adapter currently supports dockerfile deployments only",
            )

        application_payload = {
            "project_uuid": project_uuid,
            "environment_name": environment_name,
            "server_uuid": server_uuid,
            "destination_uuid": destination_uuid,
            "name": name,
            "description": f"Managed by Master Builder for tenant {tenant.tenant_id}, project {project.project_id}",
            "git_repository": project.github_repository,
            "git_branch": git_branch,
            "build_pack": "dockerfile",
            "ports_exposes": "",
            "domains": domains,
        }
        commit_sha = _normalize_optional_string(payload.commit_sha)
        if commit_sha is not None:
            application_payload["git_commit_sha"] = commit_sha

        if existing_application_uuid:
            application_uuid = existing_application_uuid
            client.update_application(
                application_uuid=application_uuid,
                payload=application_payload,
            )
        else:
            application_uuid = client.create_public_application(payload=application_payload)

        if env_payload:
            client.bulk_update_application_envs(
                application_uuid=application_uuid,
                payload={"data": env_payload},
            )
        deployment_uuid = client.start_application(application_uuid=application_uuid)
    except CoolifyApiError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Coolify deployment submission failed: {exc}",
        ) from exc

    return {
        "api_base_url": api_base_url,
        "application_uuid": application_uuid,
        "deployment_uuid": deployment_uuid,
        "git_branch": git_branch,
        "environment_name": environment_name,
    }


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
        return configured.rstrip("/")
    if tenant_plane.platform_subdomain and tenant_plane.base_domain:
        return f"https://{tenant_plane.platform_subdomain}.{tenant_plane.base_domain}/api/v1"
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Tenant deployment plane is missing api_base_url or platform domain metadata",
    )


def _normalize_git_branch(git_ref: str | None) -> str:
    normalized = _normalize_optional_string(git_ref)
    if normalized is None:
        return "main"
    prefixes = ("refs/heads/", "origin/")
    for prefix in prefixes:
        if normalized.startswith(prefix):
            branch = normalized[len(prefix):].strip()
            return branch or "main"
    return normalized


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


def _build_coolify_environment_payload(*, session, project: Project, encryption_key: str) -> list[dict[str, object]]:  # noqa: ANN001
    payload: list[dict[str, object]] = []
    for key, value in sorted((project.environment or {}).items()):
        normalized_key = _normalize_optional_string(key)
        normalized_value = _normalize_optional_string(value)
        if normalized_key is None or normalized_value is None:
            continue
        payload.append(
            {
                "key": normalized_key,
                "value": normalized_value,
                "is_literal": True,
                "is_preview": False,
            }
        )

    for key, secret_ref in sorted((project.secret_refs or {}).items()):
        normalized_key = _normalize_optional_string(key)
        normalized_secret_ref = _normalize_optional_string(secret_ref)
        if normalized_key is None or normalized_secret_ref is None:
            continue
        resolved_secret = _resolve_secret_value(
            session=session,
            secret_ref=normalized_secret_ref,
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            encryption_key=encryption_key,
        )
        if resolved_secret is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Project secret ref for {normalized_key} could not be resolved",
            )
        payload.append(
            {
                "key": normalized_key,
                "value": resolved_secret,
                "is_literal": False,
                "is_preview": False,
            }
        )
    return payload
