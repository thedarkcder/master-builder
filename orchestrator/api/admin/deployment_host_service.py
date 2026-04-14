from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.schemas import (
    DeploymentHostBootstrapRead,
    DeploymentHostCommandRead,
    DeploymentHostCreate,
    DeploymentHostRead,
    DeploymentHostRegistrationRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.deployment_host_tokens import generate_deployment_host_token, hash_deployment_host_token
from orchestrator.storage.models import DeploymentHost, DeploymentHostCommand

_ACTIVE_HOST_STATES = {"active", "degraded"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _effective_host_state(*, host: DeploymentHost) -> str:
    configured_state = str(host.state or "provisioning").strip().lower() or "provisioning"
    if configured_state == "retired":
        return "retired"
    if configured_state == "provisioning":
        return "provisioning"
    settings = get_settings()
    timeout_seconds = max(30, int(getattr(settings, "deployment_host_stale_timeout_seconds", 180)))
    last_seen = host.last_seen_at
    if last_seen is None:
        return "offline"
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    else:
        last_seen = last_seen.astimezone(timezone.utc)
    if (_now() - last_seen) > timedelta(seconds=timeout_seconds):
        return "offline"
    if configured_state in _ACTIVE_HOST_STATES:
        return configured_state
    return "degraded"


def deployment_host_to_schema(host: DeploymentHost) -> DeploymentHostRead:
    return DeploymentHostRead(
        host_id=host.host_id,
        label=host.label,
        provider=host.provider,
        infrastructure_provider=host.infrastructure_provider,
        region=host.region,
        capabilities=list(host.capability_keys_json or []),
        agent_version=host.agent_version,
        metadata=dict(host.metadata_json or {}),
        state=_effective_host_state(host=host),  # type: ignore[arg-type]
        registered_at=host.registered_at,
        last_seen_at=host.last_seen_at,
        created_at=host.created_at,
        updated_at=host.updated_at,
    )


def deployment_host_command_to_schema(
    command: DeploymentHostCommand,
    *,
    payload: dict[str, object] | None = None,
) -> DeploymentHostCommandRead:
    return DeploymentHostCommandRead(
        command_id=command.command_id,
        host_id=command.host_id,
        tenant_id=command.tenant_id,
        project_id=command.project_id,
        app_id=command.app_id,
        restore_run_id=command.restore_run_id,
        kind=command.kind,
        status=command.status,  # type: ignore[arg-type]
        claim_id=command.claim_id,
        payload=dict(payload if payload is not None else (command.payload_json or {})),
        result=dict(command.result_json or {}),
        last_error=command.last_error,
        available_at=command.available_at,
        claimed_at=command.claimed_at,
        started_at=command.started_at,
        completed_at=command.completed_at,
        created_at=command.created_at,
        updated_at=command.updated_at,
    )


def list_deployment_hosts(*, session) -> list[DeploymentHostRead]:  # noqa: ANN001
    hosts = session.execute(select(DeploymentHost).order_by(DeploymentHost.created_at.asc())).scalars().all()
    return [deployment_host_to_schema(host) for host in hosts]


def get_deployment_host(*, session, host_id: str) -> DeploymentHostRead:  # noqa: ANN001
    host = session.get(DeploymentHost, host_id)
    if host is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment host not found")
    return deployment_host_to_schema(host)


def create_deployment_host(
    *,
    session,
    payload: DeploymentHostCreate,
) -> DeploymentHostBootstrapRead:  # noqa: ANN001
    now = _now()
    bootstrap_token = generate_deployment_host_token()
    host = DeploymentHost(
        host_id=str(uuid4()),
        label=payload.label,
        provider=payload.provider,
        infrastructure_provider=payload.infrastructure_provider,
        region=payload.region,
        capability_keys_json=list(payload.capabilities),
        agent_version=None,
        metadata_json={},
        state="provisioning",
        bootstrap_token_hash=hash_deployment_host_token(bootstrap_token),
        access_token_hash=None,
        registered_at=None,
        last_seen_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(host)
    session.commit()
    session.refresh(host)
    return DeploymentHostBootstrapRead(host=deployment_host_to_schema(host), bootstrap_token=bootstrap_token)


def register_deployment_host(
    *,
    session,
    bootstrap_token: str,
    agent_version: str | None,
    advertised_capabilities: list[str],
) -> DeploymentHostRegistrationRead:  # noqa: ANN001
    bootstrap_hash = hash_deployment_host_token(bootstrap_token)
    host = session.execute(
        select(DeploymentHost).where(DeploymentHost.bootstrap_token_hash == bootstrap_hash)
    ).scalar_one_or_none()
    if host is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid deployment host bootstrap token")
    access_token = generate_deployment_host_token()
    now = _now()
    host.access_token_hash = hash_deployment_host_token(access_token)
    host.agent_version = _normalize_optional_string(agent_version)
    host.capability_keys_json = list(advertised_capabilities)
    host.state = "active"
    host.registered_at = now
    host.last_seen_at = now
    host.updated_at = now
    session.commit()
    session.refresh(host)
    return DeploymentHostRegistrationRead(host=deployment_host_to_schema(host), access_token=access_token)


def touch_deployment_host(
    *,
    session,
    host_id: str,
    agent_version: str | None,
    advertised_capabilities: list[str],
    state: str,
) -> DeploymentHostRead:  # noqa: ANN001
    host = session.get(DeploymentHost, host_id)
    if host is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Deployment host not found")
    host.agent_version = _normalize_optional_string(agent_version)
    host.capability_keys_json = list(advertised_capabilities)
    host.state = str(state or "active").strip().lower() or "active"
    host.last_seen_at = _now()
    host.updated_at = host.last_seen_at
    session.commit()
    session.refresh(host)
    return deployment_host_to_schema(host)


def resolve_active_deployment_host(
    *,
    session,
    host_id: str,
) -> DeploymentHost:
    host = session.get(DeploymentHost, host_id)
    if host is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Tenant deployment plane references an unknown managed host")
    effective_state = _effective_host_state(host=host)
    if effective_state not in _ACTIVE_HOST_STATES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Managed host '{host.label}' is not active",
        )
    return host


def resolve_default_active_deployment_host(
    *,
    session,
    provider: str = "internal_coolify",
    infrastructure_provider: str | None = None,
    region: str | None = None,
) -> DeploymentHost:
    hosts = session.execute(
        select(DeploymentHost)
        .where(DeploymentHost.provider == provider)
        .order_by(DeploymentHost.created_at.asc())
    ).scalars().all()

    active_hosts = [host for host in hosts if _effective_host_state(host=host) in _ACTIVE_HOST_STATES]
    normalized_provider = _normalize_optional_string(infrastructure_provider)
    if normalized_provider is not None:
        exact_provider_matches = [
            host
            for host in active_hosts
            if _normalize_optional_string(host.infrastructure_provider) == normalized_provider
        ]
        if not exact_provider_matches:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"No active managed host matches infrastructure provider '{normalized_provider}'",
            )
        active_hosts = exact_provider_matches

    normalized_region = _normalize_optional_string(region)
    if normalized_region is not None:
        exact_region_matches = [
            host
            for host in active_hosts
            if _normalize_optional_string(host.region) == normalized_region
        ]
        if not exact_region_matches:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"No active managed host matches region '{normalized_region}'",
            )
        active_hosts = exact_region_matches

    if not active_hosts:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="No active managed host is available for database restore")
    if len(active_hosts) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Multiple managed hosts are available; set tenant deployment plane managed_host_id explicitly",
        )
    return active_hosts[0]
