from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.admin.deployment_config_service import (
    tenant_deployment_plane_to_schema,
)
from orchestrator.api.admin.deployment_host_service import (
    resolve_active_deployment_host,
    resolve_default_active_deployment_host,
)
from orchestrator.core.deployment_host_queue import (
    DeploymentHostCommandEnqueueRequest,
    enqueue_deployment_host_command,
)
from orchestrator.storage.models import (
    DeploymentHostCommand,
    Project,
    ProjectDeploymentRelease,
    Tenant,
)

LOCAL_PREVIEW_ROUTE_SYNC_COMMAND_KIND = "sync_local_preview_routes"
LOCAL_PREVIEW_ROUTE_SYNC_CAPABILITY = "local_preview_routes"
_ACTIVE_COMMAND_STATUSES = ("queued", "claimed", "running")
_REUSABLE_COMMAND_STATUSES = ("queued", "claimed", "running", "succeeded", "failed")
_LOCAL_PREVIEW_PROXY_PORTS = {8088, 8443}


@dataclass(frozen=True)
class LocalPreviewRouteCommandState:
    command: DeploymentHostCommand | None
    deployment_uuid: str | None


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def is_local_preview_proxy_base_domain(base_domain: str | None) -> bool:
    normalized = _normalize_optional_string(base_domain)
    if normalized is None:
        return False
    parsed = urlsplit(normalized if "://" in normalized else f"//{normalized}")
    return parsed.port in _LOCAL_PREVIEW_PROXY_PORTS


def _resolve_managed_host(*, session: Session, tenant: Tenant):
    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    managed_host_id = _normalize_optional_string(tenant_plane.managed_host_id)
    if managed_host_id is not None:
        host = resolve_active_deployment_host(session=session, host_id=managed_host_id)
    else:
        host = resolve_default_active_deployment_host(
            session=session,
            provider=tenant_plane.provider,
            infrastructure_provider=tenant_plane.infrastructure_provider,
            region=tenant_plane.region,
        )
    capabilities = {
        str(item or "").strip().lower()
        for item in (host.capability_keys_json or [])
        if str(item or "").strip()
    }
    if LOCAL_PREVIEW_ROUTE_SYNC_CAPABILITY not in capabilities:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Managed host '{host.label}' does not support local preview route sync",
        )
    return host


def _command_payload(
    *,
    release: ProjectDeploymentRelease,
    deployment_uuid: str | None,
    action: str,
) -> dict[str, object]:
    provider_context = _coerce_dict(release.provider_context)
    route_bindings = provider_context.get("route_bindings")
    payload: dict[str, object] = {
        "action": action,
        "release_id": release.release_id,
        "deployment_uuid": deployment_uuid,
        "application_uuid": _normalize_optional_string(
            provider_context.get("application_uuid")
        ),
        "route_bindings": list(route_bindings)
        if isinstance(route_bindings, list)
        else [],
    }
    return payload


def latest_local_preview_route_command(
    *,
    session: Session,
    release_id: str,
) -> LocalPreviewRouteCommandState:
    command = (
        session.execute(
            select(DeploymentHostCommand)
            .where(
                DeploymentHostCommand.release_id == release_id,
                DeploymentHostCommand.kind == LOCAL_PREVIEW_ROUTE_SYNC_COMMAND_KIND,
                DeploymentHostCommand.status.in_(_REUSABLE_COMMAND_STATUSES),
            )
            .order_by(
                DeploymentHostCommand.created_at.desc(),
                DeploymentHostCommand.command_id.desc(),
            )
        )
        .scalars()
        .first()
    )
    if command is None:
        return LocalPreviewRouteCommandState(command=None, deployment_uuid=None)
    payload = _coerce_dict(command.payload_json)
    return LocalPreviewRouteCommandState(
        command=command,
        deployment_uuid=_normalize_optional_string(payload.get("deployment_uuid")),
    )


def ensure_local_preview_route_sync_command(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    release: ProjectDeploymentRelease,
    deployment_uuid: str | None,
    force: bool = False,
) -> DeploymentHostCommand | None:
    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    if release.release_kind != "run_preview" or not is_local_preview_proxy_base_domain(
        tenant_plane.base_domain
    ):
        return None
    latest = latest_local_preview_route_command(
        session=session, release_id=release.release_id
    )
    if latest.command is not None and latest.deployment_uuid == deployment_uuid:
        if not force or latest.command.status in _ACTIVE_COMMAND_STATUSES:
            return latest.command
    host = _resolve_managed_host(session=session, tenant=tenant)
    return enqueue_deployment_host_command(
        session,
        request=DeploymentHostCommandEnqueueRequest(
            host_id=host.host_id,
            kind=LOCAL_PREVIEW_ROUTE_SYNC_COMMAND_KIND,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            app_id=release.app_id,
            restore_run_id=None,
            release_id=release.release_id,
            payload_json=_command_payload(
                release=release,
                deployment_uuid=deployment_uuid,
                action="upsert",
            ),
        ),
    )


def ensure_local_preview_route_cleanup_command(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    release: ProjectDeploymentRelease,
) -> DeploymentHostCommand | None:
    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    if release.release_kind != "run_preview" or not is_local_preview_proxy_base_domain(
        tenant_plane.base_domain
    ):
        return None
    active_cleanup_command = (
        session.execute(
            select(DeploymentHostCommand)
            .where(
                DeploymentHostCommand.release_id == release.release_id,
                DeploymentHostCommand.kind == LOCAL_PREVIEW_ROUTE_SYNC_COMMAND_KIND,
                DeploymentHostCommand.status.in_(_ACTIVE_COMMAND_STATUSES),
            )
            .order_by(
                DeploymentHostCommand.created_at.desc(),
                DeploymentHostCommand.command_id.desc(),
            )
        )
        .scalars()
        .first()
    )
    if active_cleanup_command is not None:
        payload = _coerce_dict(active_cleanup_command.payload_json)
        if _normalize_optional_string(payload.get("action")) == "remove":
            return active_cleanup_command
    host = _resolve_managed_host(session=session, tenant=tenant)
    return enqueue_deployment_host_command(
        session,
        request=DeploymentHostCommandEnqueueRequest(
            host_id=host.host_id,
            kind=LOCAL_PREVIEW_ROUTE_SYNC_COMMAND_KIND,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            app_id=release.app_id,
            restore_run_id=None,
            release_id=release.release_id,
            payload_json=_command_payload(
                release=release,
                deployment_uuid=_normalize_optional_string(
                    _coerce_dict(release.provider_context).get("deployment_uuid")
                ),
                action="remove",
            ),
        ),
    )
