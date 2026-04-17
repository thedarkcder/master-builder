from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.runtime_requirements import normalize_runtime_kinds
from orchestrator.core.worker_capabilities import parse_worker_capabilities_diagnostics
from orchestrator.core.worker_capability_normalization import WorkerCapability
from orchestrator.storage.models import Run, WorkerRuntimeAuthRequest, WorkerRuntimeState

ACTIVE_RUN_STATUSES = {"queued", "dispatching", "running"}
WORKER_RUNTIME_HEARTBEAT_STALE_SECONDS = 120
OPEN_WORKER_RUNTIME_AUTH_REQUEST_STATUSES = {"pending", "active"}


@dataclass(frozen=True)
class WorkerInstanceStatusSnapshot:
    instance_id: str
    label: str
    status: str
    summary: str
    updated_at: datetime | None
    capabilities: list[str]
    active_run_count: int
    runtime_dependencies: dict[str, dict[str, object]]


@dataclass(frozen=True)
class WorkerServiceStatusSnapshot:
    status: str
    summary: str
    updated_at: datetime | None
    capabilities: list[str]
    runtime_dependencies: dict[str, dict[str, object]]
    instances: list[WorkerInstanceStatusSnapshot]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _capability_label(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "macos":
        return "macOS"
    return normalized.capitalize()


def runtime_kind_label(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "codex_cli":
        return "Codex CLI"
    if normalized == "chat_cli":
        return "Chat CLI"
    if normalized == "claude_cli":
        return "Claude CLI"
    return " ".join(part.capitalize() for part in normalized.replace("-", "_").split("_") if part)


def _worker_row_capabilities(row: WorkerRuntimeState) -> list[str]:
    capabilities, _invalid = parse_worker_capabilities_diagnostics(row.capabilities_json)
    return [_capability_label(item.value) for item in sorted(capabilities, key=lambda item: item.value)]


def _worker_row_last_seen(row: WorkerRuntimeState) -> datetime | None:
    return _coerce_aware(row.last_heartbeat_at) or _coerce_aware(row.updated_at)


def _runtime_dependencies_payload(row: WorkerRuntimeState) -> dict[str, dict[str, object]]:
    raw_value = getattr(row, "runtime_dependencies_json", None)
    if not isinstance(raw_value, dict):
        return {}
    payload: dict[str, dict[str, object]] = {}
    for raw_kind, raw_entry in raw_value.items():
        runtime_kind = str(raw_kind or "").strip().lower()
        if not runtime_kind or not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        entry["login_service_instance_id"] = row.service_instance_id
        payload[runtime_kind] = entry
    return payload


def _is_worker_row_fresh(*, row: WorkerRuntimeState, now: datetime) -> bool:
    last_seen = _worker_row_last_seen(row)
    if last_seen is None:
        return False
    return last_seen >= (now - timedelta(seconds=WORKER_RUNTIME_HEARTBEAT_STALE_SECONDS))


def _worker_instance_status(
    *,
    row: WorkerRuntimeState,
    now: datetime,
    active_run_count: int,
) -> tuple[str, str]:
    state = str(row.state or "").strip().lower()
    if state == "stopped":
        return "stopped", "Worker stopped cleanly."
    if not _is_worker_row_fresh(row=row, now=now):
        return "stale", "Last heartbeat is outside the worker freshness window."
    if state == "degraded":
        return "degraded", "Worker is online but blocked by a startup/runtime dependency."
    if active_run_count > 0 or state == "busy":
        run_label = "run" if active_run_count == 1 else "runs"
        return "busy", f"Processing {active_run_count} active {run_label}."
    if state == "starting":
        return "starting", "Worker is starting up."
    return "idle", "Worker is idle and ready for work."


def _worker_identity_key(row: WorkerRuntimeState) -> tuple[str, str]:
    agent_id = str(row.agent_id or "").strip()
    worker_mode = str(row.worker_mode or "").strip().lower()
    if agent_id:
        return agent_id, worker_mode
    return str(row.service_instance_id or "").strip(), worker_mode


def _visible_worker_rows(*, rows: list[WorkerRuntimeState], now: datetime) -> list[WorkerRuntimeState]:
    selected: dict[tuple[str, str], WorkerRuntimeState] = {}
    for row in rows:
        identity_key = _worker_identity_key(row)
        current = selected.get(identity_key)
        if current is None:
            selected[identity_key] = row
            continue
        current_last_seen = _worker_row_last_seen(current)
        row_last_seen = _worker_row_last_seen(row)
        current_fresh = _is_worker_row_fresh(row=current, now=now)
        row_fresh = _is_worker_row_fresh(row=row, now=now)
        if row_fresh and not current_fresh:
            selected[identity_key] = row
            continue
        if row_fresh == current_fresh and (row_last_seen or datetime.min.replace(tzinfo=timezone.utc)) > (
            current_last_seen or datetime.min.replace(tzinfo=timezone.utc)
        ):
            selected[identity_key] = row
    return sorted(
        selected.values(),
        key=lambda row: (
            str(row.agent_id or "").strip() or str(row.service_instance_id or "").strip(),
            str(row.worker_mode or "").strip(),
            str(row.service_instance_id or "").strip(),
        ),
    )


def _runtime_dependency_severity(state: object | None) -> int:
    normalized = str(state or "").strip().lower()
    if normalized == "ready":
        return 0
    if normalized == "degraded":
        return 1
    if normalized == "unavailable":
        return 2
    return 3


def _worker_capabilities(
    *,
    instances: list[WorkerInstanceStatusSnapshot],
    configured_capabilities: set[WorkerCapability],
) -> list[str]:
    capability_ids: set[str] = set()
    for instance in instances:
        capability_ids.update(
            {
                capability.lower().strip()
                for capability in instance.capabilities
                if str(capability).strip()
            }
        )
    if not capability_ids:
        capability_ids.update({capability.value for capability in configured_capabilities})
    return [_capability_label(item) for item in sorted(capability_ids)]


def _service_runtime_dependencies(*, instances: list[WorkerInstanceStatusSnapshot]) -> dict[str, dict[str, object]]:
    preferred_instances = [instance for instance in instances if instance.status not in {"stale", "stopped"}]
    candidate_instances = preferred_instances or instances
    grouped: dict[str, list[tuple[int, WorkerInstanceStatusSnapshot, dict[str, object]]]] = {}
    for instance in candidate_instances:
        for runtime_kind, raw_dependency in (instance.runtime_dependencies or {}).items():
            runtime_kind_key = str(runtime_kind or "").strip().lower()
            if not runtime_kind_key or not isinstance(raw_dependency, dict):
                continue
            dependency = dict(raw_dependency)
            severity = _runtime_dependency_severity(dependency.get("state"))
            grouped.setdefault(runtime_kind_key, []).append((severity, instance, dependency))
    payload: dict[str, dict[str, object]] = {}
    for runtime_kind, candidates in sorted(grouped.items()):
        blocked_candidates = [candidate for candidate in candidates if candidate[0] > 0]
        selected_candidates = blocked_candidates or candidates
        _severity, instance, dependency = max(
            selected_candidates,
            key=lambda candidate: (
                candidate[0],
                1 if str(candidate[2].get("remediation_text") or "").strip() else 0,
            ),
        )
        entry = dict(dependency)
        entry["login_service_instance_id"] = instance.instance_id
        states = {str(candidate[2].get("state") or "").strip().lower() for candidate in candidates}
        if len(states) > 1 and str(entry.get("state") or "").strip().lower() != "ready":
            entry["summary"] = (
                f"{runtime_kind_label(runtime_kind)} authentication differs across worker instances. "
                "Use the affected worker or login action to authenticate the missing runtime."
            )
        payload[runtime_kind] = entry
    return payload


def build_worker_service_status(
    *,
    session: Session,
    settings: Settings,
    now_fn=_utcnow,
) -> WorkerServiceStatusSnapshot:
    now = now_fn()
    run_counts = dict(
        session.execute(
            select(
                Run.worker_service_instance_id,
                func.count(Run.run_id),
            )
            .where(
                Run.status.in_(ACTIVE_RUN_STATUSES),
                Run.worker_service_instance_id.is_not(None),
            )
            .group_by(Run.worker_service_instance_id)
        ).all()
    )
    rows = session.execute(select(WorkerRuntimeState).order_by(WorkerRuntimeState.service_instance_id.asc())).scalars().all()
    visible_rows = _visible_worker_rows(rows=rows, now=now)
    instances: list[WorkerInstanceStatusSnapshot] = []
    for row in visible_rows:
        active_run_count = int(run_counts.get(row.service_instance_id, 0) or 0)
        instance_status, instance_summary = _worker_instance_status(
            row=row,
            now=now,
            active_run_count=active_run_count,
        )
        last_seen = _worker_row_last_seen(row)
        label = str(row.agent_id or "").strip() or row.service_instance_id
        if str(row.worker_mode or "").strip():
            label = f"{label} ({row.worker_mode})"
        instances.append(
            WorkerInstanceStatusSnapshot(
                instance_id=row.service_instance_id,
                label=label,
                status=instance_status,
                summary=instance_summary,
                updated_at=last_seen,
                capabilities=_worker_row_capabilities(row),
                active_run_count=active_run_count,
                runtime_dependencies=_runtime_dependencies_payload(row),
            )
        )

    configured_capabilities, invalid_configured_tokens = parse_worker_capabilities_diagnostics(
        getattr(settings, "worker_capabilities", None)
    )
    capabilities = _worker_capabilities(instances=instances, configured_capabilities=configured_capabilities)
    runtime_dependencies = _service_runtime_dependencies(instances=instances)
    fresh_instances = [instance for instance in instances if instance.status not in {"stale", "stopped"}]
    stale_instances = [instance for instance in instances if instance.status == "stale"]
    degraded_instances = [instance for instance in fresh_instances if instance.status == "degraded"]
    busy_instances = [instance for instance in fresh_instances if instance.status == "busy"]
    latest_heartbeat = max((instance.updated_at for instance in instances if instance.updated_at is not None), default=None)
    if fresh_instances:
        if stale_instances or degraded_instances:
            service_status = "degraded"
            parts: list[str] = []
            if stale_instances:
                parts.append(
                    f"{len(stale_instances)} worker instance{'' if len(stale_instances) == 1 else 's'} have stale heartbeats."
                )
            if degraded_instances:
                parts.append(
                    f"{len(degraded_instances)} worker instance{'' if len(degraded_instances) == 1 else 's'} are blocked by startup/runtime dependencies."
                )
            summary = " ".join(parts)
        elif busy_instances:
            busy_count = sum(instance.active_run_count for instance in busy_instances)
            summary = (
                f"{len(fresh_instances)} worker instance{'' if len(fresh_instances) == 1 else 's'} online, "
                f"{busy_count} active run{'' if busy_count == 1 else 's'} in progress."
            )
            service_status = "healthy"
        else:
            service_status = "idle"
            summary = f"{len(fresh_instances)} worker instance{'' if len(fresh_instances) == 1 else 's'} online and idle."
    elif stale_instances:
        service_status = "degraded"
        summary = f"{len(stale_instances)} worker instance{'' if len(stale_instances) == 1 else 's'} have stale heartbeats."
    elif instances:
        service_status = "unavailable"
        summary = "Worker instances are stopped."
    else:
        service_status = "unavailable"
        summary = "No worker runtime registrations are present."
    if invalid_configured_tokens:
        invalid = ", ".join(invalid_configured_tokens)
        allowed = "linux, macos"
        summary = (
            f"{summary} Invalid ORCHESTRATOR_WORKER_CAPABILITIES token(s): {invalid}. "
            f"Allowed values: {allowed}."
        )
        service_status = "degraded"
    blocked_shared_runtime_kinds = [
        runtime_kind
        for runtime_kind, dependency in runtime_dependencies.items()
        if str(dependency.get("state") or "").strip().lower() != "ready"
    ]
    if blocked_shared_runtime_kinds:
        label = ", ".join(sorted(blocked_shared_runtime_kinds))
        summary = (
            f"{summary} Shared runtime login is still required for {label}; "
            "that auth state is reused by containers that mount the same Codex home volume."
        )
    return WorkerServiceStatusSnapshot(
        status=service_status,
        summary=summary,
        updated_at=latest_heartbeat,
        capabilities=capabilities,
        runtime_dependencies=runtime_dependencies,
        instances=instances,
    )


def start_worker_runtime_login_request(
    *,
    session: Session,
    service_instance_id: str,
    runtime_kind: str,
    now_fn=_utcnow,
) -> WorkerRuntimeAuthRequest:
    normalized_service_instance_id = str(service_instance_id or "").strip()
    normalized_runtime_kind = str(runtime_kind or "").strip().lower()
    if not normalized_service_instance_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="service_instance_id is required")
    runtime_kinds = normalize_runtime_kinds((normalized_runtime_kind,))
    if not runtime_kinds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="runtime_kind is invalid")
    normalized_runtime_kind = runtime_kinds[0]
    worker_row = session.get(WorkerRuntimeState, normalized_service_instance_id)
    if worker_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker instance was not found")
    registered_runtime_kinds = set(normalize_runtime_kinds(getattr(worker_row, "runtime_kinds_json", None)))
    if normalized_runtime_kind not in registered_runtime_kinds:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Worker instance is not registered for runtime '{normalized_runtime_kind}'",
        )
    now = now_fn()
    open_requests = session.execute(
        select(WorkerRuntimeAuthRequest)
        .where(
            WorkerRuntimeAuthRequest.service_instance_id == normalized_service_instance_id,
            WorkerRuntimeAuthRequest.runtime_kind == normalized_runtime_kind,
            WorkerRuntimeAuthRequest.status.in_(tuple(sorted(OPEN_WORKER_RUNTIME_AUTH_REQUEST_STATUSES))),
        )
        .order_by(WorkerRuntimeAuthRequest.requested_at.desc())
    ).scalars().all()
    for request in open_requests:
        request.status = "cancelled"
        request.completed_at = now
        request.last_error = "Superseded by a newer login session request."

    auth_request = WorkerRuntimeAuthRequest(
        request_id=str(uuid4()),
        service_instance_id=normalized_service_instance_id,
        runtime_kind=normalized_runtime_kind,
        status="pending",
        remediation_text=None,
        requested_at=now,
        started_at=None,
        completed_at=None,
        expires_at=None,
        last_error=None,
    )
    session.add(auth_request)
    session.commit()
    session.refresh(auth_request)
    return auth_request


def get_worker_runtime_login_request(
    *,
    session: Session,
    request_id: str,
) -> WorkerRuntimeAuthRequest:
    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="request_id is required")
    auth_request = session.get(WorkerRuntimeAuthRequest, normalized_request_id)
    if auth_request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Worker runtime auth request was not found")
    return auth_request
