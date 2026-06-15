from __future__ import annotations

import asyncio
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.api.admin.deployment_config_service import tenant_deployment_plane_to_schema
from orchestrator.api.admin.deployment_release_service import (
    _normalize_local_coolify_api_base_url,
    update_project_deployment_release_status,
    verify_release_route_bindings,
)
from orchestrator.api.schemas import ProjectDeploymentReleaseStatusUpdate, TenantDeploymentPlaneRead
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.local_preview_route_sync import (
    ensure_local_preview_route_sync_command,
    latest_local_preview_route_command,
)
from orchestrator.core.deployment_status import DEPLOYMENT_RELEASE_ACTIVE_STATUSES
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.secret_manager import normalize_secret_ref, resolve_scoped_secret_ref
from orchestrator.storage.models import Project, ProjectDeploymentRelease, Tenant
from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig, CoolifyApiError

logger = logging.getLogger(__name__)

_DEPLOYMENT_PROGRESS_ORDER = ("queued", "provisioning", "deploying", "route_activating", "live")
_DEPLOYMENT_FAILURE_STATUSES = {"failed", "error", "crashed", "failure", "timeout", "unhealthy"}
_DEPLOYMENT_CANCELLED_STATUSES = {"cancelled", "canceled", "rollback", "rolled_back"}
_DEPLOYMENT_SUCCESS_STATUSES = {"success", "succeeded", "complete", "completed", "done", "finished", "healthy"}
_DEPLOYMENT_PROGRESS_STATUSES = {"queued", "provisioning", "deploying", "building", "running", "starting", "started", "pending", "preparing"}


@dataclass(frozen=True)
class CoolifyDeploymentObservation:
    deployment_uuid: str | None
    application_uuid: str | None
    status: str
    application_status: str | None = None
    provider_updated_at: datetime | None = None
    last_error: str | None = None


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _parse_provider_datetime(value: object) -> datetime | None:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        return None
    candidate = normalized.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _release_activity_anchor(
    *,
    release: ProjectDeploymentRelease,
    observation: CoolifyDeploymentObservation,
) -> datetime | None:
    for candidate in (observation.provider_updated_at, release.started_at, release.requested_at, release.created_at):
        if candidate is None:
            continue
        if candidate.tzinfo is None:
            return candidate.replace(tzinfo=timezone.utc)
        return candidate.astimezone(timezone.utc)
    return None


def _stale_active_release_error(
    *,
    release: ProjectDeploymentRelease,
    observation: CoolifyDeploymentObservation,
    settings: Settings,
    now: datetime,
) -> str | None:
    current = _normalize_release_status(release.status)
    observed = _normalize_release_status(observation.status)
    if current not in DEPLOYMENT_RELEASE_ACTIVE_STATUSES:
        return None
    if observed not in _DEPLOYMENT_PROGRESS_STATUSES and "deploy" not in (observed or "") and "progress" not in (
        observed or ""
    ):
        return None
    timeout_seconds = max(60, int(settings.deployment_release_stale_timeout_seconds))
    anchor = _release_activity_anchor(release=release, observation=observation)
    if anchor is None or now.astimezone(timezone.utc) - anchor <= timedelta(seconds=timeout_seconds):
        return None
    return (
        f"Deployment provider remained in progress status '{observation.status}' for more than "
        f"{timeout_seconds} seconds without reaching a terminal release state."
    )


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


def _resolve_secret_value(
    *,
    session: Session,
    secret_ref: str,
    tenant_id: str,
    project_id: str | None,
    encryption_key: str,
) -> str | None:
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


def _deployment_release_query(session: Session) -> list[ProjectDeploymentRelease]:
    return session.execute(
        select(ProjectDeploymentRelease)
        .where(ProjectDeploymentRelease.status.in_(tuple(DEPLOYMENT_RELEASE_ACTIVE_STATUSES)))
        .order_by(ProjectDeploymentRelease.created_at.asc())
    ).scalars().all()


def _tenant_for_release(session: Session, *, release: ProjectDeploymentRelease) -> Tenant | None:
    tenant = session.get(Tenant, release.tenant_id)
    if tenant is None:
        return None
    return tenant


def _project_for_release(session: Session, *, release: ProjectDeploymentRelease) -> Project | None:
    project = session.get(Project, release.project_id)
    if project is None or project.tenant_id != release.tenant_id:
        return None
    return project


def _coolify_client_for_tenant(
    *,
    session: Session,
    tenant: Tenant,
    tenant_plane: TenantDeploymentPlaneRead,
) -> CoolifyApiClient:
    settings = _settings_from_session(session=session)
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
        project_id=None,
        encryption_key=encryption_key,
    )
    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane references missing Coolify API token",
        )
    return CoolifyApiClient(
        CoolifyApiConfig(
            base_url=_coolify_api_base_url(tenant_plane=tenant_plane),
            bearer_token=api_token,
        )
    )


def _settings_from_session(*, session: Session) -> Settings:
    _ = session
    return get_settings()


def _coolify_observation_for_release(
    *,
    client: CoolifyApiClient,
    release: ProjectDeploymentRelease,
) -> CoolifyDeploymentObservation | None:
    provider_context = _coerce_dict(release.provider_context)
    deployment_uuid = _normalize_optional_string(provider_context.get("deployment_uuid"))
    application_uuid = _normalize_optional_string(provider_context.get("application_uuid"))
    service_uuid = _normalize_optional_string(provider_context.get("service_uuid"))

    payload: dict[str, object] | None = None
    if deployment_uuid is not None:
        payload = client.get_deployment(deployment_uuid=deployment_uuid)
    elif application_uuid is not None:
        deployments = client.list_application_deployments(application_uuid=application_uuid)
        if deployments:
            payload = deployments[0]
    elif service_uuid is not None:
        payload = client.get_service(service_uuid=service_uuid)
    if not payload:
        return None

    observed_status = _normalize_optional_string(payload.get("status"))
    if observed_status is None:
        return None
    application_payload = _coerce_dict(payload.get("application"))
    application_status = _normalize_optional_string(application_payload.get("status"))
    observed_deployment_uuid = _normalize_optional_string(payload.get("deployment_uuid")) or deployment_uuid
    observed_application_uuid = (
        _normalize_optional_string(payload.get("application_id"))
        or _normalize_optional_string(application_payload.get("uuid"))
        or application_uuid
    )
    normalized_observed_status = _normalize_release_status(observed_status)
    last_error = (
        _normalize_optional_string(payload.get("logs"))
        if normalized_observed_status in _DEPLOYMENT_FAILURE_STATUSES
        else None
    )
    return CoolifyDeploymentObservation(
        deployment_uuid=observed_deployment_uuid,
        application_uuid=observed_application_uuid,
        status=observed_status,
        application_status=application_status,
        provider_updated_at=_parse_provider_datetime(payload.get("updated_at") or payload.get("created_at")),
        last_error=last_error,
    )


def list_reconcilable_deployment_releases(*, session: Session) -> list[ProjectDeploymentRelease]:
    return _deployment_release_query(session)


def reconcile_deployment_release(
    *,
    session: Session,
    release: ProjectDeploymentRelease,
) -> bool:
    tenant = _tenant_for_release(session, release=release)
    project = _project_for_release(session, release=release)
    if tenant is None or project is None:
        return False

    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    if tenant_plane.provider != "internal_coolify" or tenant_plane.state not in {"active", "degraded"}:
        return False

    client = _coolify_client_for_tenant(session=session, tenant=tenant, tenant_plane=tenant_plane)
    try:
        observation = _coolify_observation_for_release(client=client, release=release)
    except CoolifyApiError as exc:
        logger.warning(
            "deployment_reconcile_coolify_poll_failed tenant_id=%s project_id=%s release_id=%s error=%s",
            release.tenant_id,
            release.project_id,
            release.release_id,
            exc,
        )
        return False

    if observation is None:
        return False

    current_status = str(release.status or "").strip()
    stale_error = _stale_active_release_error(
        release=release,
        observation=observation,
        settings=_settings_from_session(session=session),
        now=datetime.now(timezone.utc),
    )
    if stale_error is not None:
        updated = update_project_deployment_release_status(
            session=session,
            tenant_id=release.tenant_id,
            project_id=release.project_id,
            release_id=release.release_id,
            app_id=release.app_id,
            payload=ProjectDeploymentReleaseStatusUpdate(
                status="failed",
                last_error=stale_error,
                deployment_uuid=observation.deployment_uuid,
            ),
        )
        logger.warning(
            "deployment_reconcile_stale_provider_progress tenant_id=%s project_id=%s release_id=%s from_status=%s to_status=%s observed_status=%s",
            release.tenant_id,
            release.project_id,
            release.release_id,
            current_status,
            updated.status,
            observation.status,
        )
        return True

    next_status = _resolve_release_observation_transition(
        current_status=current_status,
        observed_status=observation.status,
        observed_application_status=observation.application_status,
    )
    if next_status is None:
        return False
    last_error = observation.last_error
    if next_status == "live":
        verification = verify_release_route_bindings(release)
        if not verification.ok:
            latest_route_command = latest_local_preview_route_command(session=session, release_id=release.release_id)
            if str(release.release_kind or "").strip() == "run_preview":
                if latest_route_command.command is None:
                    ensure_local_preview_route_sync_command(
                        session=session,
                        tenant=tenant,
                        project=project,
                        release=release,
                        deployment_uuid=observation.deployment_uuid,
                    )
                    next_status = "route_activating"
                    last_error = verification.error
                elif latest_route_command.deployment_uuid != observation.deployment_uuid:
                    ensure_local_preview_route_sync_command(
                        session=session,
                        tenant=tenant,
                        project=project,
                        release=release,
                        deployment_uuid=observation.deployment_uuid,
                    )
                    next_status = "route_activating"
                    last_error = verification.error
                elif latest_route_command.command.status in {"queued", "claimed", "running"}:
                    next_status = "route_activating"
                    last_error = verification.error
                elif latest_route_command.command.status == "failed":
                    next_status = "failed"
                    last_error = (
                        f"Local preview route sync failed: {latest_route_command.command.last_error}"
                        if latest_route_command.command.last_error
                        else "Local preview route sync failed"
                    )
                else:
                    next_status = "failed"
                    last_error = f"Local preview route remained inactive after sync: {verification.error}"
            else:
                next_status = "route_activating"
                last_error = verification.error

    updated = update_project_deployment_release_status(
        session=session,
        tenant_id=release.tenant_id,
        project_id=release.project_id,
        release_id=release.release_id,
        app_id=release.app_id,
        payload=ProjectDeploymentReleaseStatusUpdate(
            status=next_status,
            last_error=last_error,
            deployment_uuid=observation.deployment_uuid,
        ),
    )
    logger.info(
        "deployment_reconcile_advanced tenant_id=%s project_id=%s release_id=%s from_status=%s to_status=%s observed_status=%s",
        release.tenant_id,
        release.project_id,
        release.release_id,
        current_status,
        updated.status,
        observation.status,
    )
    return True


def reconcile_deployment_releases_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    limit: int | None = None,
) -> int:
    _ = settings
    processed = 0
    batch_size = max(1, int(limit or 25))
    with session_factory() as session:
        releases = list_reconcilable_deployment_releases(session=session)[:batch_size]
        for release in releases:
            try:
                if reconcile_deployment_release(session=session, release=release):
                    processed += 1
            except HTTPException as exc:
                logger.warning(
                    "deployment_reconcile_release_skipped tenant_id=%s project_id=%s release_id=%s status_code=%s detail=%s",
                    release.tenant_id,
                    release.project_id,
                    release.release_id,
                    exc.status_code,
                    exc.detail,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "deployment_reconcile_release_failed tenant_id=%s project_id=%s release_id=%s error=%s",
                    release.tenant_id,
                    release.project_id,
                    release.release_id,
                    exc,
                )
        session.commit()
    return processed


async def run_deployment_reconciliation_loop(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    poll_interval_seconds = max(5, int(getattr(settings, "deployment_reconcile_poll_seconds", 30)))
    while not stop_event.is_set():
        try:
            processed = await asyncio.to_thread(
                reconcile_deployment_releases_once,
                session_factory=session_factory,
                settings=settings,
            )
            if processed:
                logger.info("deployment_reconcile_cycle_completed processed=%s", processed)
        except Exception:
            logger.exception("deployment_reconcile_cycle_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except asyncio.TimeoutError:
            continue


def _extract_webhook_secret_ref(tenant_plane: TenantDeploymentPlaneRead) -> str | None:
    secret_ref = str((tenant_plane.secret_refs or {}).get("coolify_webhook_token") or "").strip()
    return secret_ref or None


def _resolve_webhook_token(
    *,
    session: Session,
    tenant: Tenant,
    tenant_plane: TenantDeploymentPlaneRead,
    project_id: str,
) -> str:
    settings = _settings_from_session(session=session)
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    secret_ref = _extract_webhook_secret_ref(tenant_plane)
    if secret_ref is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is missing coolify_webhook_token secret ref",
        )
    token = _resolve_secret_value(
        session=session,
        secret_ref=secret_ref,
        tenant_id=tenant.tenant_id,
        project_id=project_id,
        encryption_key=encryption_key,
    )
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane references missing Coolify webhook token",
        )
    return token


def _extract_event_status(payload: dict[str, object]) -> str | None:
    for key in ("status", "event", "event_type", "type", "state"):
        normalized = _normalize_optional_string(payload.get(key))
        if normalized is not None:
            return normalized
    return None


def _extract_event_identifier(payload: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        normalized = _normalize_optional_string(payload.get(key))
        if normalized is not None:
            return normalized
    return None


def _extract_event_error(payload: dict[str, object]) -> str | None:
    for key in ("error", "message", "logs", "reason", "detail"):
        normalized = _normalize_optional_string(payload.get(key))
        if normalized is not None:
            return normalized
    return None


def _normalize_release_status(value: object) -> str | None:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _next_progress_status(current_status: str) -> str | None:
    current = _normalize_release_status(current_status)
    if current is None:
        return "provisioning"
    for index, candidate in enumerate(_DEPLOYMENT_PROGRESS_ORDER):
        if candidate == current:
            if index + 1 < len(_DEPLOYMENT_PROGRESS_ORDER):
                return _DEPLOYMENT_PROGRESS_ORDER[index + 1]
            return None
    return "provisioning"


def _resolve_release_observation_transition(
    *,
    current_status: str,
    observed_status: object | None,
    observed_application_status: object | None = None,
) -> str | None:
    current = _normalize_release_status(current_status)
    if current in {"failed", "rolled_back"}:
        return None

    observed = _normalize_release_status(observed_status)
    application = _normalize_release_status(observed_application_status)
    if observed is None:
        return None

    if observed in _DEPLOYMENT_PROGRESS_STATUSES or "deploy" in observed or "progress" in observed or observed == "in_progress":
        if current in {"queued", "provisioning", "deploying", "route_activating", "live"}:
            return "deploying" if current != "live" else None
        return "provisioning"

    if observed in _DEPLOYMENT_SUCCESS_STATUSES or "success" in observed:
        if application in _DEPLOYMENT_FAILURE_STATUSES or "unhealthy" in (application or ""):
            return "rolled_back" if current == "live" else "failed"
        return "live"

    if observed in _DEPLOYMENT_FAILURE_STATUSES or "unhealthy" in observed:
        return "rolled_back" if current == "live" else "failed"
    if observed in _DEPLOYMENT_CANCELLED_STATUSES:
        return "rolled_back"

    return None


def _resolve_release_status_transition(
    *,
    current_status: str,
    event_type: object | None = None,
    observed_status: object | None = None,
) -> str | None:
    current = _normalize_release_status(current_status)
    if current in {"failed", "rolled_back"}:
        return None

    observed = _normalize_release_status(observed_status)
    event = _normalize_release_status(event_type)
    signal = observed or event
    if signal is None:
        return None

    if signal in _DEPLOYMENT_FAILURE_STATUSES or "unhealthy" in signal:
        return "rolled_back" if current == "live" else "failed"
    if signal in _DEPLOYMENT_CANCELLED_STATUSES:
        return "rolled_back"
    if signal in _DEPLOYMENT_SUCCESS_STATUSES or "success" in signal or "healthy" in signal:
        if current == "deploying":
            return "live"
        return _next_progress_status(current or "queued")
    if signal == "running" or signal.startswith("running:"):
        if current == "deploying":
            return "live"
        return _next_progress_status(current or "queued")
    if signal in _DEPLOYMENT_PROGRESS_STATUSES or "deploy" in signal or "progress" in signal or "status" in signal or "container" in signal:
        return _next_progress_status(current or "queued")
    return None


def _select_release_for_event(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    deployment_uuid: str | None,
    application_uuid: str | None,
) -> ProjectDeploymentRelease | None:
    if deployment_uuid is None and application_uuid is None:
        return None
    releases = session.execute(
        select(ProjectDeploymentRelease)
        .where(
            ProjectDeploymentRelease.tenant_id == tenant_id,
            ProjectDeploymentRelease.project_id == project_id,
            ProjectDeploymentRelease.provider == "internal_coolify",
        )
        .order_by(ProjectDeploymentRelease.created_at.desc())
    ).scalars().all()
    if not releases:
        return None

    for release in releases:
        provider_context = _coerce_dict(release.provider_context)
        release_deployment_uuid = _normalize_optional_string(provider_context.get("deployment_uuid"))
        release_application_uuid = _normalize_optional_string(provider_context.get("application_uuid"))
        if deployment_uuid is not None and release_deployment_uuid == deployment_uuid:
            return release
        if application_uuid is not None and release_application_uuid == application_uuid:
            return release
    return None


def ingest_coolify_deployment_event(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    webhook_token: str,
    payload: dict[str, object],
) -> dict[str, object]:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    tenant_plane = tenant_deployment_plane_to_schema(tenant)
    if tenant_plane.provider != "internal_coolify":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Tenant deployment plane is not configured for internal Coolify",
        )
    expected_token = _resolve_webhook_token(
        session=session,
        tenant=tenant,
        tenant_plane=tenant_plane,
        project_id=project_id,
    )
    if not webhook_token or not secrets.compare_digest(webhook_token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Coolify webhook token")

    observed_status = _extract_event_status(payload)
    event_type = _extract_event_identifier(payload, "event_type", "event", "type", "status")
    deployment_uuid = _extract_event_identifier(payload, "deployment_uuid", "uuid")
    application_uuid = _extract_event_identifier(payload, "application_uuid", "application_id")
    release = _select_release_for_event(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        deployment_uuid=deployment_uuid,
        application_uuid=application_uuid,
    )
    if release is None:
        return {
            "ok": True,
            "updated": False,
            "reason": "no_matching_release",
        }

    next_status = _resolve_release_status_transition(
        current_status=release.status,
        event_type=event_type,
        observed_status=observed_status,
    )
    if next_status is None:
        return {
            "ok": True,
            "updated": False,
            "reason": "no_transition",
            "release_id": release.release_id,
            "status": release.status,
        }
    last_error = _extract_event_error(payload)
    if next_status == "live":
        verification = verify_release_route_bindings(release)
        if not verification.ok:
            next_status = "route_activating"
            last_error = verification.error

    updated = update_project_deployment_release_status(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        release_id=release.release_id,
        app_id=release.app_id,
        payload=ProjectDeploymentReleaseStatusUpdate(
            status=next_status,
            deployment_uuid=deployment_uuid,
            last_error=last_error,
        ),
    )
    return {
        "ok": True,
        "updated": True,
        "release_id": updated.release_id,
        "status": updated.status,
    }
