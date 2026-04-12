from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.agent_execution_profiles import (
    RUNTIME_KIND_CHAT_CLI,
    RUNTIME_KIND_CLAUDE_CLI,
    RUNTIME_KIND_CODEX_CLI,
    list_supported_runtime_kinds,
    runtime_kind_label,
)
from orchestrator.core.codex_runtime import codex_device_auth_instructions, codex_login_status
from orchestrator.core.config import Settings
from orchestrator.core.runtime_requirements import normalize_runtime_kinds
from orchestrator.storage.models import WorkerRuntimeState

RUNTIME_DEPENDENCY_STATE_READY = "ready"
RUNTIME_DEPENDENCY_STATE_DEGRADED = "degraded"
RUNTIME_DEPENDENCY_STATE_UNAVAILABLE = "unavailable"


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
            payload["remediation_expires_at"] = self.remediation_expires_at.astimezone(timezone.utc).isoformat()
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
        return {runtime_kind: dependency.as_json() for runtime_kind, dependency in sorted(self.dependencies.items())}


def runtime_dependency_payload_to_json(raw_value: object | None) -> dict[str, dict[str, Any]]:
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
    dependencies: dict[str, RuntimeDependencyStatus] = {}
    for runtime_kind in sorted(registered_runtime_kinds):
        dependencies[runtime_kind] = _check_runtime_dependency(
            runtime_kind=runtime_kind,
            settings=settings,
            existing_payload=existing_payload.get(runtime_kind, {}),
        )
    return WorkerRuntimeDependencySnapshot(dependencies=dependencies)


def _check_runtime_dependency(
    *,
    runtime_kind: str,
    settings: Settings,
    existing_payload: dict[str, Any] | None,
) -> RuntimeDependencyStatus:
    if runtime_kind == RUNTIME_KIND_CODEX_CLI:
        return _check_codex_cli_dependency(settings=settings, existing_payload=existing_payload or {})
    if runtime_kind in {RUNTIME_KIND_CHAT_CLI, RUNTIME_KIND_CLAUDE_CLI}:
        return _check_cli_command_dependency(runtime_kind=runtime_kind, settings=settings)
    return RuntimeDependencyStatus(
        runtime_kind=runtime_kind,
        state=RUNTIME_DEPENDENCY_STATE_READY,
        summary=f"{runtime_kind_label(runtime_kind)} does not require worker-local runtime login.",
    )


def _check_cli_command_dependency(*, runtime_kind: str, settings: Settings) -> RuntimeDependencyStatus:
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
    settings: Settings,
    existing_payload: dict[str, Any],
) -> RuntimeDependencyStatus:
    command = _runtime_cli_command(runtime_kind=RUNTIME_KIND_CODEX_CLI, settings=settings)
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
        seconds=max(60, int(getattr(settings, "worker_runtime_auth_remediation_ttl_seconds", 900)))
    )
    existing_expires_at = _parse_iso_datetime(existing_payload.get("remediation_expires_at"))
    remediation_text = str(existing_payload.get("remediation_text") or "").strip()
    if not remediation_text or existing_expires_at is None or existing_expires_at <= now:
        remediation_text = codex_device_auth_instructions(
            codex_command=command,
            settings=settings,
            working_dir=None,
        ).strip()
    else:
        expires_at = existing_expires_at
    return RuntimeDependencyStatus(
        runtime_kind=RUNTIME_KIND_CODEX_CLI,
        state=RUNTIME_DEPENDENCY_STATE_DEGRADED,
        summary="Codex CLI is not authenticated on this worker.",
        remediation_text=remediation_text or auth_status_output or None,
        remediation_expires_at=expires_at,
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
    return sorted(set(normalize_runtime_kinds(getattr(settings, "worker_runtime_kinds", None))))


def _registered_worker_runtime_kinds(raw_value: object | None, *, settings: Settings) -> set[str]:
    registered = set(normalize_runtime_kinds(raw_value))
    if registered:
        return registered
    return set(registered_worker_runtime_kinds_from_settings(settings))
