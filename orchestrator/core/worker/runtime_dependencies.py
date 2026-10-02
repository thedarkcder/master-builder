from __future__ import annotations

import subprocess
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.runtime.agent_execution_profiles import (
    RUNTIME_KIND_CHAT_CLI,
    RUNTIME_KIND_CLAUDE_CLI,
    RUNTIME_KIND_CODEX_CLI,
    list_supported_runtime_kinds,
    runtime_kind_label,
)
from orchestrator.core.runtime.runtime import build_cli_command_env, codex_login_status
from orchestrator.core.config import Settings
from orchestrator.core.runtime.requirements import normalize_runtime_kinds
from orchestrator.storage.models import WorkerRuntimeAuthRequest, WorkerRuntimeState

RUNTIME_DEPENDENCY_STATE_READY = "ready"
RUNTIME_DEPENDENCY_STATE_DEGRADED = "degraded"
RUNTIME_DEPENDENCY_STATE_UNAVAILABLE = "unavailable"
WORKER_RUNTIME_AUTH_REQUEST_STATUS_PENDING = "pending"
WORKER_RUNTIME_AUTH_REQUEST_STATUS_ACTIVE = "active"
WORKER_RUNTIME_AUTH_REQUEST_STATUS_COMPLETED = "completed"
WORKER_RUNTIME_AUTH_REQUEST_STATUS_CANCELLED = "cancelled"
WORKER_RUNTIME_AUTH_REQUEST_STATUS_FAILED = "failed"
WORKER_RUNTIME_AUTH_REQUEST_STATUS_EXPIRED = "expired"


@dataclass
class _LiveRuntimeAuthSession:
    request_id: str
    process: subprocess.Popen[str]
    started_at: datetime
    expires_at: datetime
    _buffer: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append(self, text: str) -> None:
        normalized = str(text or "")
        if not normalized:
            return
        with self._lock:
            self._buffer.append(normalized)

    def remediation_text(self) -> str:
        with self._lock:
            return "".join(self._buffer).strip()


_LIVE_RUNTIME_AUTH_SESSIONS: dict[tuple[str, str], _LiveRuntimeAuthSession] = {}
_LIVE_RUNTIME_AUTH_SESSIONS_LOCK = threading.Lock()


@dataclass(frozen=True)
class RuntimeDependencyStatus:
    runtime_kind: str
    state: str
    summary: str
    remediation_text: str | None = None
    remediation_expires_at: datetime | None = None

    def as_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "state": self.state,
            "summary": self.summary,
        }
        if self.remediation_text:
            payload["remediation_text"] = self.remediation_text
        if self.remediation_expires_at is not None:
            payload["remediation_expires_at"] = self.remediation_expires_at.astimezone(
                timezone.utc
            ).isoformat()
        return payload


@dataclass(frozen=True)
class WorkerRuntimeDependencySnapshot:
    dependencies: dict[str, RuntimeDependencyStatus]

    @property
    def ready_runtime_kinds(self) -> set[str]:
        return {
            runtime_kind
            for runtime_kind, dependency in self.dependencies.items()
            if dependency.state == RUNTIME_DEPENDENCY_STATE_READY
        }

    @property
    def blocked_runtime_kinds(self) -> set[str]:
        return {
            runtime_kind
            for runtime_kind, dependency in self.dependencies.items()
            if dependency.state != RUNTIME_DEPENDENCY_STATE_READY
        }

    @property
    def degraded(self) -> bool:
        return bool(self.blocked_runtime_kinds)

    def to_json(self) -> dict[str, dict[str, Any]]:
        return {
            runtime_kind: dependency.as_json()
            for runtime_kind, dependency in sorted(self.dependencies.items())
        }


def runtime_dependency_payload_to_json(
    raw_value: object | None,
) -> dict[str, dict[str, Any]]:
    if not isinstance(raw_value, dict):
        return {}
    payload: dict[str, dict[str, Any]] = {}
    for raw_kind, raw_entry in raw_value.items():
        runtime_kind = str(raw_kind or "").strip().lower()
        if runtime_kind not in list_supported_runtime_kinds():
            continue
        if not isinstance(raw_entry, dict):
            continue
        payload[runtime_kind] = dict(raw_entry)
    return payload


def worker_runtime_dependency_snapshot(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    service_instance_id: str,
) -> WorkerRuntimeDependencySnapshot:
    with session_factory() as session:
        row = session.get(WorkerRuntimeState, service_instance_id)
        registered_runtime_kinds = _registered_worker_runtime_kinds(
            getattr(row, "runtime_kinds_json", None) if row is not None else None,
            settings=settings,
        )
        existing_payload = runtime_dependency_payload_to_json(
            getattr(row, "runtime_dependencies_json", None) if row is not None else None
        )
        auth_requests = _open_worker_runtime_auth_requests(
            session=session,
            service_instance_id=service_instance_id,
        )
    dependencies: dict[str, RuntimeDependencyStatus] = {}
    for runtime_kind in sorted(registered_runtime_kinds):
        dependencies[runtime_kind] = _check_runtime_dependency(
            session_factory=session_factory,
            service_instance_id=service_instance_id,
            runtime_kind=runtime_kind,
            settings=settings,
            existing_payload=existing_payload.get(runtime_kind, {}),
            auth_request=auth_requests.get(runtime_kind),
        )
    return WorkerRuntimeDependencySnapshot(dependencies=dependencies)


def _check_runtime_dependency(
    *,
    session_factory: sessionmaker[Session],
    service_instance_id: str,
    runtime_kind: str,
    settings: Settings,
    existing_payload: dict[str, Any] | None,
    auth_request: WorkerRuntimeAuthRequest | None,
) -> RuntimeDependencyStatus:
    if runtime_kind == RUNTIME_KIND_CODEX_CLI:
        return _check_codex_cli_dependency(
            session_factory=session_factory,
            service_instance_id=service_instance_id,
            settings=settings,
            existing_payload=existing_payload or {},
            auth_request=auth_request,
        )
    if runtime_kind in {RUNTIME_KIND_CHAT_CLI, RUNTIME_KIND_CLAUDE_CLI}:
        return _check_cli_command_dependency(
            runtime_kind=runtime_kind, settings=settings
        )
    return RuntimeDependencyStatus(
        runtime_kind=runtime_kind,
        state=RUNTIME_DEPENDENCY_STATE_READY,
        summary=f"{runtime_kind_label(runtime_kind)} does not require worker-local runtime login.",
    )


def _check_cli_command_dependency(
    *, runtime_kind: str, settings: Settings
) -> RuntimeDependencyStatus:
    command = _runtime_cli_command(runtime_kind=runtime_kind, settings=settings)
    if not command:
        return RuntimeDependencyStatus(
            runtime_kind=runtime_kind,
            state=RUNTIME_DEPENDENCY_STATE_UNAVAILABLE,
            summary=f"{runtime_kind_label(runtime_kind)} command is not configured.",
        )
    if shutil.which(command) is None:
        return RuntimeDependencyStatus(
            runtime_kind=runtime_kind,
            state=RUNTIME_DEPENDENCY_STATE_UNAVAILABLE,
            summary=f"{runtime_kind_label(runtime_kind)} command '{command}' was not found in PATH.",
        )
    return RuntimeDependencyStatus(
        runtime_kind=runtime_kind,
        state=RUNTIME_DEPENDENCY_STATE_READY,
        summary=f"{runtime_kind_label(runtime_kind)} is installed and ready on this worker.",
    )


def _check_codex_cli_dependency(
    *,
    session_factory: sessionmaker[Session],
    service_instance_id: str,
    settings: Settings,
    existing_payload: dict[str, Any],
    auth_request: WorkerRuntimeAuthRequest | None,
) -> RuntimeDependencyStatus:
    command = _runtime_cli_command(
        runtime_kind=RUNTIME_KIND_CODEX_CLI, settings=settings
    )
    if not command:
        return RuntimeDependencyStatus(
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
            state=RUNTIME_DEPENDENCY_STATE_UNAVAILABLE,
            summary="Codex CLI command is not configured.",
        )
    if shutil.which(command) is None:
        return RuntimeDependencyStatus(
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
            state=RUNTIME_DEPENDENCY_STATE_UNAVAILABLE,
            summary=f"Codex CLI command '{command}' was not found in PATH.",
        )
    auth_ready, auth_status_output = codex_login_status(
        codex_command=command,
        settings=settings,
        working_dir=None,
    )
    if auth_ready is True:
        _complete_worker_runtime_auth_request(
            session_factory=session_factory,
            service_instance_id=service_instance_id,
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
        )
        _stop_live_runtime_auth_session(
            service_instance_id=service_instance_id,
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
        )
        return RuntimeDependencyStatus(
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
            state=RUNTIME_DEPENDENCY_STATE_READY,
            summary="Codex CLI is authenticated and ready.",
        )
    if auth_ready is None:
        return RuntimeDependencyStatus(
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
            state=RUNTIME_DEPENDENCY_STATE_UNAVAILABLE,
            summary=auth_status_output or "Codex CLI status check failed.",
        )
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(
        seconds=max(
            60,
            int(getattr(settings, "worker_runtime_auth_remediation_ttl_seconds", 900)),
        )
    )
    remediation_text: str | None = None
    active_request = auth_request
    if (
        active_request is not None
        and active_request.status == WORKER_RUNTIME_AUTH_REQUEST_STATUS_PENDING
    ):
        active_request = _start_codex_cli_login_session(
            session_factory=session_factory,
            service_instance_id=service_instance_id,
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
            request_id=active_request.request_id,
            settings=settings,
            expires_at=expires_at,
        )
    if (
        active_request is not None
        and active_request.status == WORKER_RUNTIME_AUTH_REQUEST_STATUS_ACTIVE
    ):
        active_request = _refresh_live_runtime_auth_request(
            session_factory=session_factory,
            service_instance_id=service_instance_id,
            runtime_kind=RUNTIME_KIND_CODEX_CLI,
            request_id=active_request.request_id,
            settings=settings,
        )
    if active_request is not None:
        remediation_text = str(active_request.remediation_text or "").strip() or None
        expires_at = active_request.expires_at or expires_at
    return RuntimeDependencyStatus(
        runtime_kind=RUNTIME_KIND_CODEX_CLI,
        state=RUNTIME_DEPENDENCY_STATE_DEGRADED,
        summary="Codex CLI is not authenticated on this worker.",
        remediation_text=remediation_text,
        remediation_expires_at=expires_at if remediation_text else None,
    )


def _runtime_cli_command(*, runtime_kind: str, settings: Settings) -> str:
    if runtime_kind == RUNTIME_KIND_CODEX_CLI:
        return str(getattr(settings, "codex_cli_command", "") or "").strip()
    if runtime_kind == RUNTIME_KIND_CHAT_CLI:
        return str(getattr(settings, "chat_cli_command", "") or "").strip()
    if runtime_kind == RUNTIME_KIND_CLAUDE_CLI:
        return str(getattr(settings, "claude_cli_command", "") or "").strip()
    return ""


def _parse_iso_datetime(value: object | None) -> datetime | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def registered_worker_runtime_kinds_from_settings(settings: Settings) -> list[str]:
    return sorted(
        set(normalize_runtime_kinds(getattr(settings, "worker_runtime_kinds", None)))
    )


def _registered_worker_runtime_kinds(
    raw_value: object | None, *, settings: Settings
) -> set[str]:
    registered = set(normalize_runtime_kinds(raw_value))
    if registered:
        return registered
    return set(registered_worker_runtime_kinds_from_settings(settings))


def _open_worker_runtime_auth_requests(
    *,
    session: Session,
    service_instance_id: str,
) -> dict[str, WorkerRuntimeAuthRequest]:
    rows = (
        session.execute(
            select(WorkerRuntimeAuthRequest)
            .where(
                WorkerRuntimeAuthRequest.service_instance_id == service_instance_id,
                WorkerRuntimeAuthRequest.status.in_(
                    (
                        WORKER_RUNTIME_AUTH_REQUEST_STATUS_PENDING,
                        WORKER_RUNTIME_AUTH_REQUEST_STATUS_ACTIVE,
                    )
                ),
            )
            .order_by(WorkerRuntimeAuthRequest.requested_at.desc())
        )
        .scalars()
        .all()
    )
    selected: dict[str, WorkerRuntimeAuthRequest] = {}
    for row in rows:
        if row.runtime_kind not in selected:
            selected[row.runtime_kind] = row
    return selected


def _read_process_stream(process_stream: Any, session_key: tuple[str, str]) -> None:
    try:
        while True:
            line = process_stream.readline()
            if not line:
                break
            with _LIVE_RUNTIME_AUTH_SESSIONS_LOCK:
                session = _LIVE_RUNTIME_AUTH_SESSIONS.get(session_key)
            if session is None:
                break
            session.append(line)
    finally:
        try:
            process_stream.close()
        except Exception:
            pass


def _start_codex_cli_login_session(
    *,
    session_factory: sessionmaker[Session],
    service_instance_id: str,
    runtime_kind: str,
    request_id: str,
    settings: Settings,
    expires_at: datetime,
) -> WorkerRuntimeAuthRequest | None:
    session_key = (service_instance_id, runtime_kind)
    with _LIVE_RUNTIME_AUTH_SESSIONS_LOCK:
        current_session = _LIVE_RUNTIME_AUTH_SESSIONS.get(session_key)
    if (
        current_session is None
        or current_session.request_id != request_id
        or current_session.process.poll() is not None
    ):
        command_cwd, env = build_cli_command_env(
            settings=settings, working_dir=None, runtime_kind=runtime_kind
        )
        process = subprocess.Popen(  # noqa: S603
            [
                _runtime_cli_command(runtime_kind=runtime_kind, settings=settings),
                "login",
                "--device-auth",
            ],
            cwd=command_cwd or None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        current_session = _LiveRuntimeAuthSession(
            request_id=request_id,
            process=process,
            started_at=datetime.now(timezone.utc),
            expires_at=expires_at,
        )
        with _LIVE_RUNTIME_AUTH_SESSIONS_LOCK:
            _LIVE_RUNTIME_AUTH_SESSIONS[session_key] = current_session
        if process.stdout is not None:
            reader_thread = threading.Thread(
                target=_read_process_stream,
                args=(process.stdout, session_key),
                daemon=True,
            )
            reader_thread.start()
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        while datetime.now(timezone.utc) < deadline:
            if current_session.remediation_text():
                break
            if process.poll() is not None:
                break
            threading.Event().wait(0.1)
    return _update_worker_runtime_auth_request(
        session_factory=session_factory,
        request_id=request_id,
        status=WORKER_RUNTIME_AUTH_REQUEST_STATUS_ACTIVE,
        remediation_text=current_session.remediation_text(),
        started_at=current_session.started_at,
        expires_at=current_session.expires_at,
        last_error=None,
    )


def _refresh_live_runtime_auth_request(
    *,
    session_factory: sessionmaker[Session],
    service_instance_id: str,
    runtime_kind: str,
    request_id: str,
    settings: Settings,
) -> WorkerRuntimeAuthRequest | None:
    session_key = (service_instance_id, runtime_kind)
    now = datetime.now(timezone.utc)
    with _LIVE_RUNTIME_AUTH_SESSIONS_LOCK:
        live_session = _LIVE_RUNTIME_AUTH_SESSIONS.get(session_key)
    if live_session is None or live_session.request_id != request_id:
        return _update_worker_runtime_auth_request(
            session_factory=session_factory,
            request_id=request_id,
            status=WORKER_RUNTIME_AUTH_REQUEST_STATUS_FAILED,
            completed_at=now,
            last_error="Worker login session is no longer running.",
        )
    auth_ready, auth_status_output = codex_login_status(
        codex_command=_runtime_cli_command(
            runtime_kind=runtime_kind, settings=settings
        ),
        settings=settings,
        working_dir=None,
    )
    if auth_ready is True:
        _stop_live_runtime_auth_session(
            service_instance_id=service_instance_id, runtime_kind=runtime_kind
        )
        return _update_worker_runtime_auth_request(
            session_factory=session_factory,
            request_id=request_id,
            status=WORKER_RUNTIME_AUTH_REQUEST_STATUS_COMPLETED,
            remediation_text=live_session.remediation_text(),
            completed_at=now,
            expires_at=live_session.expires_at,
            last_error=None,
        )
    if live_session.expires_at <= now:
        _stop_live_runtime_auth_session(
            service_instance_id=service_instance_id, runtime_kind=runtime_kind
        )
        return _update_worker_runtime_auth_request(
            session_factory=session_factory,
            request_id=request_id,
            status=WORKER_RUNTIME_AUTH_REQUEST_STATUS_EXPIRED,
            completed_at=now,
            remediation_text=live_session.remediation_text(),
            expires_at=live_session.expires_at,
            last_error="Worker login session expired before authentication completed.",
        )
    process_return_code = live_session.process.poll()
    if process_return_code is not None:
        _stop_live_runtime_auth_session(
            service_instance_id=service_instance_id, runtime_kind=runtime_kind
        )
        return _update_worker_runtime_auth_request(
            session_factory=session_factory,
            request_id=request_id,
            status=WORKER_RUNTIME_AUTH_REQUEST_STATUS_FAILED,
            completed_at=now,
            remediation_text=live_session.remediation_text(),
            expires_at=live_session.expires_at,
            last_error=auth_status_output
            or f"Worker login session exited with code {process_return_code}.",
        )
    return _update_worker_runtime_auth_request(
        session_factory=session_factory,
        request_id=request_id,
        status=WORKER_RUNTIME_AUTH_REQUEST_STATUS_ACTIVE,
        remediation_text=live_session.remediation_text(),
        started_at=live_session.started_at,
        expires_at=live_session.expires_at,
        last_error=None,
    )


def _update_worker_runtime_auth_request(
    *,
    session_factory: sessionmaker[Session],
    request_id: str,
    status: str,
    remediation_text: str | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    expires_at: datetime | None = None,
    last_error: str | None = None,
) -> WorkerRuntimeAuthRequest | None:
    with session_factory() as session:
        row = session.get(WorkerRuntimeAuthRequest, request_id)
        if row is None:
            return None
        row.status = status
        if remediation_text is not None:
            row.remediation_text = remediation_text
        if started_at is not None:
            row.started_at = started_at
        if completed_at is not None:
            row.completed_at = completed_at
        if expires_at is not None:
            row.expires_at = expires_at
        row.last_error = last_error
        session.commit()
        session.refresh(row)
        return row


def _complete_worker_runtime_auth_request(
    *,
    session_factory: sessionmaker[Session],
    service_instance_id: str,
    runtime_kind: str,
) -> None:
    with session_factory() as session:
        rows = (
            session.execute(
                select(WorkerRuntimeAuthRequest).where(
                    WorkerRuntimeAuthRequest.service_instance_id == service_instance_id,
                    WorkerRuntimeAuthRequest.runtime_kind == runtime_kind,
                    WorkerRuntimeAuthRequest.status.in_(
                        (
                            WORKER_RUNTIME_AUTH_REQUEST_STATUS_PENDING,
                            WORKER_RUNTIME_AUTH_REQUEST_STATUS_ACTIVE,
                        )
                    ),
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return
        now = datetime.now(timezone.utc)
        for row in rows:
            row.status = WORKER_RUNTIME_AUTH_REQUEST_STATUS_COMPLETED
            row.completed_at = now
            row.last_error = None
        session.commit()


def _stop_live_runtime_auth_session(
    *, service_instance_id: str, runtime_kind: str
) -> None:
    session_key = (service_instance_id, runtime_kind)
    with _LIVE_RUNTIME_AUTH_SESSIONS_LOCK:
        live_session = _LIVE_RUNTIME_AUTH_SESSIONS.pop(session_key, None)
    if live_session is None:
        return
    if live_session.process.poll() is None:
        try:
            live_session.process.terminate()
            live_session.process.wait(timeout=2)
        except Exception:
            try:
                live_session.process.kill()
            except Exception:
                pass


def stop_all_live_runtime_auth_sessions() -> None:
    with _LIVE_RUNTIME_AUTH_SESSIONS_LOCK:
        session_keys = list(_LIVE_RUNTIME_AUTH_SESSIONS.keys())
    for service_instance_id, runtime_kind in session_keys:
        _stop_live_runtime_auth_session(
            service_instance_id=service_instance_id, runtime_kind=runtime_kind
        )


def sync_worker_runtime_auth_requests(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    service_instance_id: str,
) -> None:
    with session_factory() as session:
        row = session.get(WorkerRuntimeState, service_instance_id)
        registered_runtime_kinds = _registered_worker_runtime_kinds(
            getattr(row, "runtime_kinds_json", None) if row is not None else None,
            settings=settings,
        )
        auth_requests = _open_worker_runtime_auth_requests(
            session=session,
            service_instance_id=service_instance_id,
        )

    for runtime_kind, auth_request in auth_requests.items():
        if runtime_kind not in registered_runtime_kinds:
            continue
        if runtime_kind != RUNTIME_KIND_CODEX_CLI:
            continue
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=max(
                60,
                int(
                    getattr(
                        settings, "worker_runtime_auth_remediation_ttl_seconds", 900
                    )
                ),
            )
        )
        if auth_request.status == WORKER_RUNTIME_AUTH_REQUEST_STATUS_PENDING:
            _start_codex_cli_login_session(
                session_factory=session_factory,
                service_instance_id=service_instance_id,
                runtime_kind=runtime_kind,
                request_id=auth_request.request_id,
                settings=settings,
                expires_at=expires_at,
            )
            continue
        if auth_request.status == WORKER_RUNTIME_AUTH_REQUEST_STATUS_ACTIVE:
            _refresh_live_runtime_auth_request(
                session_factory=session_factory,
                service_instance_id=service_instance_id,
                runtime_kind=runtime_kind,
                request_id=auth_request.request_id,
                settings=settings,
            )
