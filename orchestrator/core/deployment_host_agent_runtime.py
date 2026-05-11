from __future__ import annotations

import json
import logging
import os
import shlex
import signal
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.deployment_restore_executor import (
    DeploymentRestoreExecutionContext,
    build_docker_exec_restore_command_text,
)
from orchestrator.core.observability.logging import configure_logging

logger = logging.getLogger(__name__)


class DeploymentHostAgentError(RuntimeError):
    pass


class DeploymentHostControlPlaneError(DeploymentHostAgentError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DeploymentHostAgentConfig:
    api_base_url: str
    bootstrap_token: str | None
    bootstrap_token_path: Path | None
    access_token: str | None
    access_token_path: Path
    capabilities: tuple[str, ...]
    heartbeat_interval_seconds: int
    poll_interval_seconds: int
    command_timeout_seconds: int
    agent_version: str
    container_runtime_command: str


@dataclass(frozen=True)
class DeploymentHostAgentResult:
    processed: bool
    command_id: str | None = None


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_capabilities(raw_value: object) -> tuple[str, ...]:
    if isinstance(raw_value, str):
        values = raw_value.replace("\n", ",").split(",")
    elif isinstance(raw_value, (list, tuple, set)):
        values = list(raw_value)
    else:
        values = []
    normalized: list[str] = []
    for item in values:
        capability = str(item or "").strip().lower()
        if capability and capability not in normalized:
            normalized.append(capability)
    return tuple(normalized)


def _resolve_access_token_path(settings: Settings) -> Path:
    explicit = _normalize_optional_string(getattr(settings, "deployment_host_agent_access_token_path", ""))
    if explicit is not None:
        return Path(explicit).expanduser()
    runtime_home = _normalize_optional_string(getattr(settings, "runtime_home", ""))
    if runtime_home is not None:
        return Path(runtime_home).expanduser() / "deployment-host-agent-access-token"
    return Path.home() / ".master-builder" / "deployment-host-agent-access-token"


def resolve_deployment_host_agent_config(*, settings: Settings | None = None) -> DeploymentHostAgentConfig:
    resolved_settings = settings or get_settings()
    api_base_url = (
        _normalize_optional_string(getattr(resolved_settings, "deployment_host_agent_api_base_url", ""))
        or _normalize_optional_string(getattr(resolved_settings, "public_api_base_url", ""))
    )
    if api_base_url is None:
        raise DeploymentHostAgentError("deployment_host_agent_api_base_url or public_api_base_url is required")
    return DeploymentHostAgentConfig(
        api_base_url=api_base_url.rstrip("/"),
        bootstrap_token=_normalize_optional_string(getattr(resolved_settings, "deployment_host_agent_bootstrap_token", "")),
        bootstrap_token_path=(
            Path(raw_bootstrap_path).expanduser()
            if (raw_bootstrap_path := _normalize_optional_string(
                getattr(resolved_settings, "deployment_host_agent_bootstrap_token_path", ""),
            )) is not None
            else None
        ),
        access_token=_normalize_optional_string(getattr(resolved_settings, "deployment_host_agent_access_token", "")),
        access_token_path=_resolve_access_token_path(resolved_settings),
        capabilities=_normalize_capabilities(getattr(resolved_settings, "deployment_host_agent_capabilities", "")),
        heartbeat_interval_seconds=max(
            5,
            int(getattr(resolved_settings, "deployment_host_agent_heartbeat_interval_seconds", 30)),
        ),
        poll_interval_seconds=max(1, int(getattr(resolved_settings, "deployment_host_agent_poll_seconds", 5))),
        command_timeout_seconds=max(
            30,
            int(getattr(resolved_settings, "deployment_host_agent_command_timeout_seconds", 900)),
        ),
        agent_version=_normalize_optional_string(getattr(resolved_settings, "sentry_release", "")) or "dev-local",
        container_runtime_command=(
            _normalize_optional_string(getattr(resolved_settings, "deployment_host_agent_container_runtime_command", "docker"))
            or "docker"
        ),
    )


def load_deployment_host_agent_access_token(path: Path) -> str | None:
    try:
        return _normalize_optional_string(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def load_deployment_host_agent_bootstrap_token(path: Path) -> str | None:
    try:
        return _normalize_optional_string(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def persist_deployment_host_agent_access_token(path: Path, access_token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(access_token, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def clear_deployment_host_agent_access_token(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


class DeploymentHostControlPlaneClient:
    def __init__(
        self,
        *,
        api_base_url: str,
        access_token: str | None = None,
        urlopen_fn=urlopen,
    ) -> None:
        self._api_base_url = api_base_url.rstrip("/")
        self._access_token = _normalize_optional_string(access_token)
        self._urlopen = urlopen_fn

    def with_access_token(self, access_token: str) -> DeploymentHostControlPlaneClient:
        return DeploymentHostControlPlaneClient(
            api_base_url=self._api_base_url,
            access_token=access_token,
            urlopen_fn=self._urlopen,
        )

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        access_token: str | None = None,
    ) -> dict[str, object]:
        url = f"{self._api_base_url}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "master-builder-deployment-host-agent",
        }
        bearer_token = _normalize_optional_string(access_token) or self._access_token
        if bearer_token is not None:
            headers["Authorization"] = f"Bearer {bearer_token}"
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with self._urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            detail = error_body.strip() or exc.reason
            raise DeploymentHostControlPlaneError(
                f"Deployment host control-plane request failed ({exc.code}) for {method} {path}: {detail}",
                status_code=int(exc.code),
            ) from exc
        if not response_body.strip():
            return {}
        parsed = json.loads(response_body)
        if not isinstance(parsed, dict):
            raise DeploymentHostControlPlaneError(
                f"Deployment host control-plane response for {method} {path} was not an object",
            )
        return parsed

    def register(
        self,
        *,
        bootstrap_token: str,
        agent_version: str,
        advertised_capabilities: tuple[str, ...],
    ) -> dict[str, object]:
        return self._request_json(
            method="POST",
            path="/api/internal/deployment-hosts/register",
            payload={
                "bootstrap_token": bootstrap_token,
                "agent_version": agent_version,
                "advertised_capabilities": list(advertised_capabilities),
            },
        )

    def heartbeat(
        self,
        *,
        agent_version: str,
        advertised_capabilities: tuple[str, ...],
        state: str,
    ) -> dict[str, object]:
        return self._request_json(
            method="POST",
            path="/api/internal/deployment-hosts/heartbeat",
            payload={
                "agent_version": agent_version,
                "advertised_capabilities": list(advertised_capabilities),
                "state": state,
            },
        )

    def claim_command(self) -> dict[str, object] | None:
        response = self._request_json(
            method="POST",
            path="/api/internal/deployment-hosts/commands/claim",
            payload={},
        )
        command = response.get("command")
        if command is None:
            return None
        if not isinstance(command, dict):
            raise DeploymentHostControlPlaneError("Deployment host claim response returned a non-object command")
        return dict(command)

    def start_command(self, *, command_id: str, claim_id: str) -> dict[str, object]:
        return self._request_json(
            method="POST",
            path=f"/api/internal/deployment-hosts/commands/{command_id}/start",
            payload={"claim_id": claim_id},
        )

    def complete_command(
        self,
        *,
        command_id: str,
        claim_id: str,
        status: str,
        result: dict[str, object],
        last_error: str | None,
    ) -> dict[str, object]:
        return self._request_json(
            method="POST",
            path=f"/api/internal/deployment-hosts/commands/{command_id}/result",
            payload={
                "claim_id": claim_id,
                "status": status,
                "result": result,
                "last_error": last_error,
            },
        )


def _truncate_output(value: object, *, limit: int = 2000) -> str | None:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        return None
    if len(normalized) <= limit:
        return normalized
    return normalized[-limit:]


def _execution_context_from_payload(payload: dict[str, object]) -> DeploymentRestoreExecutionContext:
    execution_context = payload.get("execution_context")
    if not isinstance(execution_context, dict):
        raise DeploymentHostAgentError("Restore command payload is missing execution_context")
    try:
        return DeploymentRestoreExecutionContext(
            database_type=str(execution_context["database_type"]),
            container_name=str(execution_context["container_name"]),
            database_name=str(execution_context["database_name"]),
            username=str(execution_context["username"]),
            password=str(execution_context["password"]),
            host=str(execution_context["host"]),
            port=int(execution_context["port"]),
            artifact_path=str(execution_context["artifact_path"]),
        )
    except KeyError as exc:
        raise DeploymentHostAgentError(f"Restore command payload is missing {exc.args[0]}") from exc


def _container_candidates_from_payload(payload: dict[str, object]) -> tuple[str, ...]:
    raw_candidates = payload.get("container_candidates")
    if not isinstance(raw_candidates, list):
        raise DeploymentHostAgentError("Restore command payload is missing container_candidates")
    candidates: list[str] = []
    for raw_value in raw_candidates:
        candidate = _normalize_optional_string(raw_value)
        if candidate is not None and candidate not in candidates:
            candidates.append(candidate)
    if not candidates:
        raise DeploymentHostAgentError("Restore command payload has no usable container candidates")
    return tuple(candidates)


def execute_restore_command(
    *,
    payload: dict[str, object],
    container_runtime_command: str,
    timeout_seconds: int,
    subprocess_run_fn=subprocess.run,
) -> tuple[str, dict[str, object], str | None]:
    execution_context = _execution_context_from_payload(payload)
    candidates = _container_candidates_from_payload(payload)
    attempts: list[dict[str, object]] = []
    for candidate in candidates:
        command_text = build_docker_exec_restore_command_text(
            context=execution_context,
            container_reference=shlex.quote(candidate),
            container_runtime_command=container_runtime_command,
        )
        try:
            completed = subprocess_run_fn(
                ["sh", "-lc", command_text],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            attempts.append(
                {
                    "container": candidate,
                    "status": "timeout",
                    "timeout_seconds": timeout_seconds,
                }
            )
            continue
        attempt_result = {
            "container": candidate,
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "return_code": int(completed.returncode),
            "stdout_tail": _truncate_output(completed.stdout),
            "stderr_tail": _truncate_output(completed.stderr),
        }
        attempts.append(attempt_result)
        if completed.returncode == 0:
            return (
                "succeeded",
                {
                    "selected_container": candidate,
                    "attempts": attempts,
                    "database_type": execution_context.database_type,
                    "resource_key": _normalize_optional_string(payload.get("resource_key")),
                    "restore_run_id": _normalize_optional_string(payload.get("restore_run_id")),
                },
                None,
            )
    return (
        "failed",
        {
            "attempts": attempts,
            "database_type": execution_context.database_type,
            "resource_key": _normalize_optional_string(payload.get("resource_key")),
            "restore_run_id": _normalize_optional_string(payload.get("restore_run_id")),
        },
        "Restore command failed for every container candidate",
    )


class DeploymentHostAgent:
    def __init__(
        self,
        *,
        config: DeploymentHostAgentConfig,
        client_factory=DeploymentHostControlPlaneClient,
        subprocess_run_fn=subprocess.run,
    ) -> None:
        self._config = config
        self._client_factory = client_factory
        self._subprocess_run_fn = subprocess_run_fn
        self._client: DeploymentHostControlPlaneClient | None = None
        self._last_heartbeat_at: float | None = None
        self._health_state = "active"

    def _load_access_token(self) -> str | None:
        return self._config.access_token or load_deployment_host_agent_access_token(self._config.access_token_path)

    def _load_bootstrap_token(self) -> str | None:
        if self._config.bootstrap_token is not None:
            return self._config.bootstrap_token
        if self._config.bootstrap_token_path is None:
            return None
        return load_deployment_host_agent_bootstrap_token(self._config.bootstrap_token_path)

    def _ensure_client(self) -> DeploymentHostControlPlaneClient:
        if self._client is not None:
            return self._client
        access_token = self._load_access_token()
        if access_token is None:
            bootstrap_token = self._load_bootstrap_token()
            if bootstrap_token is None:
                raise DeploymentHostAgentError(
                    "Deployment host agent requires either an access token or a bootstrap token",
                )
            anonymous_client = self._client_factory(api_base_url=self._config.api_base_url, access_token=None)
            registration = anonymous_client.register(
                bootstrap_token=bootstrap_token,
                agent_version=self._config.agent_version,
                advertised_capabilities=self._config.capabilities,
            )
            registered_access_token = _normalize_optional_string(registration.get("access_token"))
            if registered_access_token is None:
                raise DeploymentHostAgentError("Deployment host registration did not return an access token")
            persist_deployment_host_agent_access_token(self._config.access_token_path, registered_access_token)
            access_token = registered_access_token
            logger.info("deployment_host_agent_registered")
        self._client = self._client_factory(
            api_base_url=self._config.api_base_url,
            access_token=access_token,
        )
        return self._client

    def _reset_access_token(self) -> None:
        clear_deployment_host_agent_access_token(self._config.access_token_path)
        self._client = None

    def _call_control_plane(self, callback, *, allow_reauth: bool = True):  # noqa: ANN001
        try:
            return callback(self._ensure_client())
        except DeploymentHostControlPlaneError as exc:
            if not allow_reauth or exc.status_code != 401:
                raise
            if self._load_bootstrap_token() is None:
                raise
            logger.warning("deployment_host_agent_reauth_requested status_code=%s", exc.status_code)
            self._reset_access_token()
            return callback(self._ensure_client())

    def _heartbeat_if_due(self, *, force: bool = False) -> None:
        now = monotonic()
        if not force and self._last_heartbeat_at is not None:
            elapsed = now - self._last_heartbeat_at
            if elapsed < float(self._config.heartbeat_interval_seconds):
                return
        self._call_control_plane(
            lambda client: client.heartbeat(
                agent_version=self._config.agent_version,
                advertised_capabilities=self._config.capabilities,
                state=self._health_state,
            )
        )
        self._last_heartbeat_at = now

    def process_once(self) -> DeploymentHostAgentResult:
        self._heartbeat_if_due(force=self._last_heartbeat_at is None)
        command = self._call_control_plane(lambda active_client: active_client.claim_command())
        if command is None:
            return DeploymentHostAgentResult(processed=False)

        command_id = _normalize_optional_string(command.get("command_id"))
        claim_id = _normalize_optional_string(command.get("claim_id"))
        kind = _normalize_optional_string(command.get("kind"))
        if command_id is None or claim_id is None or kind is None:
            raise DeploymentHostAgentError("Claimed deployment host command is missing identifiers")
        command_payload = command.get("payload")
        if not isinstance(command_payload, dict):
            raise DeploymentHostAgentError("Claimed deployment host command is missing payload")

        self._call_control_plane(lambda active_client: active_client.start_command(command_id=command_id, claim_id=claim_id))
        if kind != "restore_database":
            result = {"kind": kind}
            self._call_control_plane(
                lambda active_client: active_client.complete_command(
                    command_id=command_id,
                    claim_id=claim_id,
                    status="failed",
                    result=result,
                    last_error=f"Unsupported deployment host command kind '{kind}'",
                )
            )
            self._health_state = "degraded"
            return DeploymentHostAgentResult(processed=True, command_id=command_id)

        status, result, last_error = execute_restore_command(
            payload=command_payload,
            container_runtime_command=self._config.container_runtime_command,
            timeout_seconds=self._config.command_timeout_seconds,
            subprocess_run_fn=self._subprocess_run_fn,
        )
        self._call_control_plane(
            lambda active_client: active_client.complete_command(
                command_id=command_id,
                claim_id=claim_id,
                status=status,
                result=result,
                last_error=last_error,
            )
        )
        self._health_state = "active" if status == "succeeded" else "degraded"
        return DeploymentHostAgentResult(processed=True, command_id=command_id)

    def run(self, *, stop_event: threading.Event | None = None) -> None:
        local_stop_event = stop_event or threading.Event()

        def _handle_signal(_signum: int, _frame) -> None:  # noqa: ANN001
            local_stop_event.set()

        previous_sigint = signal.getsignal(signal.SIGINT)
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        try:
            while not local_stop_event.is_set():
                result = self.process_once()
                if result.processed:
                    continue
                local_stop_event.wait(timeout=float(self._config.poll_interval_seconds))
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
            signal.signal(signal.SIGTERM, previous_sigterm)


def run_deployment_host_agent(*, settings: Settings | None = None) -> None:
    resolved_settings = settings or get_settings()
    configure_logging(
        resolved_settings.log_level,
        environment=resolved_settings.sentry_environment,
        platform_version=resolved_settings.sentry_release or "dev-local",
        default_agent_id=str(resolved_settings.agent_id or "").strip() or "deployment-host-agent",
    )
    agent = DeploymentHostAgent(config=resolve_deployment_host_agent_config(settings=resolved_settings))
    logger.info("deployment_host_agent_started")
    agent.run()
