from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestrator.storage.models import DeploymentHostCommand

_LEASE_DURATION = timedelta(minutes=5)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_postgres(session: Session) -> bool:
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


@dataclass(frozen=True)
class DeploymentHostCommandEnqueueRequest:
    host_id: str
    kind: str
    tenant_id: str
    project_id: str | None
    app_id: str | None
    restore_run_id: str | None
    release_id: str | None
    payload_json: dict[str, object]


def enqueue_deployment_host_command(
    session: Session,
    *,
    request: DeploymentHostCommandEnqueueRequest,
    now: datetime | None = None,
) -> DeploymentHostCommand:
    timestamp = now or _now()
    tenant_id = str(request.tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("deployment host commands require tenant_id")
    command = DeploymentHostCommand(
        command_id=str(uuid4()),
        host_id=request.host_id,
        tenant_id=tenant_id,
        project_id=request.project_id,
        app_id=request.app_id,
        restore_run_id=request.restore_run_id,
        release_id=request.release_id,
        kind=request.kind,
        status="queued",
        claim_id=None,
        lease_expires_at=None,
        available_at=timestamp,
        attempt_count=0,
        payload_json=dict(request.payload_json or {}),
        result_json={},
        last_error=None,
        claimed_at=None,
        started_at=None,
        completed_at=None,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(command)
    session.flush()
    session.refresh(command)
    return command


def claim_next_deployment_host_command(
    session: Session,
    *,
    host_id: str,
    now: datetime | None = None,
) -> DeploymentHostCommand | None:
    timestamp = now or _now()
    query = (
        select(DeploymentHostCommand)
        .where(
            DeploymentHostCommand.host_id == host_id,
            DeploymentHostCommand.available_at <= timestamp,
            or_(
                DeploymentHostCommand.status == "queued",
                (
                    (DeploymentHostCommand.status == "claimed")
                    & (DeploymentHostCommand.lease_expires_at.is_not(None))
                    & (DeploymentHostCommand.lease_expires_at <= timestamp)
                ),
            ),
        )
        .order_by(
            DeploymentHostCommand.available_at.asc(),
            DeploymentHostCommand.created_at.asc(),
        )
        .limit(1)
    )
    if _is_postgres(session):
        query = query.with_for_update(skip_locked=True)
    command = session.execute(query).scalar_one_or_none()
    if command is None:
        return None
    command.status = "claimed"
    command.claim_id = str(uuid4())
    command.claimed_at = timestamp
    command.lease_expires_at = timestamp + _LEASE_DURATION
    command.attempt_count = int(command.attempt_count or 0) + 1
    command.last_error = None
    command.updated_at = timestamp
    session.flush()
    session.refresh(command)
    return command


def start_deployment_host_command(
    session: Session,
    *,
    host_id: str,
    command_id: str,
    claim_id: str,
    lease_duration: timedelta | None = None,
    now: datetime | None = None,
) -> DeploymentHostCommand:
    timestamp = now or _now()
    effective_lease_duration = lease_duration or _LEASE_DURATION
    command = session.get(DeploymentHostCommand, command_id)
    if command is None or command.host_id != host_id:
        raise RuntimeError("Deployment host command was not found")
    if str(command.claim_id or "").strip() != str(claim_id or "").strip():
        raise RuntimeError("Deployment host command claim is invalid")
    if command.status not in {"claimed", "running"}:
        raise RuntimeError(
            f"Deployment host command cannot start from status '{command.status}'"
        )
    if command.status != "running":
        command.status = "running"
        command.started_at = timestamp
        command.lease_expires_at = timestamp + effective_lease_duration
        command.updated_at = timestamp
        session.flush()
        session.refresh(command)
    return command


def fail_stale_running_deployment_host_commands(
    session: Session,
    *,
    host_id: str | None = None,
    now: datetime | None = None,
    error_message: str = "Deployment host command expired while running",
) -> list[DeploymentHostCommand]:
    timestamp = now or _now()
    predicates = [
        DeploymentHostCommand.status == "running",
        DeploymentHostCommand.lease_expires_at.is_not(None),
        DeploymentHostCommand.lease_expires_at <= timestamp,
    ]
    if host_id is not None:
        predicates.append(DeploymentHostCommand.host_id == host_id)
    query = (
        select(DeploymentHostCommand)
        .where(*predicates)
        .order_by(
            DeploymentHostCommand.lease_expires_at.asc(),
            DeploymentHostCommand.created_at.asc(),
        )
    )
    if _is_postgres(session):
        query = query.with_for_update(skip_locked=True)
    commands = list(session.execute(query).scalars().all())
    for command in commands:
        command.status = "failed"
        command.completed_at = timestamp
        command.updated_at = timestamp
        command.lease_expires_at = None
        command.last_error = error_message
        if command.started_at is None:
            command.started_at = timestamp
    if commands:
        session.flush()
    return commands


def complete_deployment_host_command(
    session: Session,
    *,
    host_id: str,
    command_id: str,
    claim_id: str,
    status: str,
    result_json: dict[str, object] | None = None,
    last_error: str | None = None,
    now: datetime | None = None,
) -> DeploymentHostCommand:
    timestamp = now or _now()
    command = session.get(DeploymentHostCommand, command_id)
    if command is None or command.host_id != host_id:
        raise RuntimeError("Deployment host command was not found")
    if str(command.claim_id or "").strip() != str(claim_id or "").strip():
        raise RuntimeError("Deployment host command claim is invalid")
    if command.status not in {"claimed", "running"}:
        raise RuntimeError(
            f"Deployment host command cannot complete from status '{command.status}'"
        )
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"succeeded", "failed"}:
        raise RuntimeError("Deployment host command completion status is invalid")
    command.status = normalized_status
    if command.started_at is None:
        command.started_at = timestamp
    command.completed_at = timestamp
    command.updated_at = timestamp
    command.lease_expires_at = None
    command.result_json = dict(result_json or {})
    command.last_error = str(last_error or "").strip() or None
    session.flush()
    session.refresh(command)
    return command
