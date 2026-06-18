from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.api.admin.deployment_release_service import get_project_deployment_release_logs
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.runtime.agents import CodexWorkflowAgents
from orchestrator.core.runtime.runtime import build_codex_runtime
from orchestrator.core.runtime.tools import execute_agent_tool
from orchestrator.core.qa.mobile_xcuitest_recorder import preferred_simulator_udid
from orchestrator.core.worker.capability_normalization import parse_worker_capability
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    QaFailureEvidence,
    QaRecording,
    QaResult,
    QaScenario,
    QaStep,
    ReviewResult,
    TestResult,
    WorkflowRequest,
)
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.repo_allowlist import normalize_repo_identifier
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref

DEMO_EVIDENCE_HEADING = "## Demo Evidence"
DEMO_EVIDENCE_MARKER = "<!-- master-builder:qa-demo-evidence v1 -->"
DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER = "<!-- master-builder:qa-demo-required-targets"
DEMO_EVIDENCE_REQUIRED_COUNTS_MARKER = "<!-- master-builder:qa-demo-required-counts"
DEMO_FAILURE_EVIDENCE_HEADING = "## Demo Failure Evidence"
DEMO_FAILURE_EVIDENCE_MARKER = "<!-- master-builder:qa-demo-failure-evidence v1 -->"
_TRANSIENT_NATIVE_SELECTORS = frozenset({"id=splash_screen", "splash_screen"})
_DEMO_CAPTURE_TARGETS = frozenset({"browser", "ios", "android", "desktop"})
_NATIVE_CAPTURE_TARGETS = frozenset({"ios", "android", "desktop"})
_RELEASE_SERVICE_READY_STATUSES = frozenset({401, 403, 405})
_QA_PROOF_STEP_ACTIONS = frozenset({"assert_visible", "assert_text", "wait_for_text", "wait_for_url"})
_QA_DEMO_CONTENT_SHA256_METADATA_KEY = "content-sha256"
_QA_DEMO_CONTENT_SHA256_METADATA_HEADER = f"x-amz-meta-{_QA_DEMO_CONTENT_SHA256_METADATA_KEY}"
_QA_DEMO_RELEASE_CONTEXT_SHA256_METADATA_KEY = "release-context-sha256"
_QA_DEMO_RELEASE_CONTEXT_SHA256_METADATA_HEADER = f"x-amz-meta-{_QA_DEMO_RELEASE_CONTEXT_SHA256_METADATA_KEY}"
_QA_DEMO_RELEASE_COMMIT_SHA_METADATA_KEY = "release-commit-sha"
_QA_DEMO_RELEASE_COMMIT_SHA_METADATA_HEADER = f"x-amz-meta-{_QA_DEMO_RELEASE_COMMIT_SHA_METADATA_KEY}"
_MIN_DEMO_REQUIREMENT_VARIANTS = 2


@dataclass(frozen=True)
class DemoArtifactStorageConfig:
    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    public_base_url: str
    secure: bool


@dataclass(frozen=True)
class LocalQaRecording:
    name: str
    path: str
    capture_target: str
    capture_reference: str
    content_type: str
    content_sha256: str


@dataclass(frozen=True)
class LocalQaFailureEvidence:
    name: str
    path: str
    capture_target: str
    capture_reference: str
    content_type: str
    content_sha256: str
    error_message: str


class QaDemoRecordingFailure(RuntimeError):
    def __init__(self, message: str, *, failure_evidence: list[LocalQaFailureEvidence] | None = None) -> None:
        super().__init__(message)
        self.failure_evidence = list(failure_evidence or [])


@dataclass(frozen=True)
class DemoCaptureTarget:
    capture_target: str
    capture_reference: str
    recorder_command: tuple[str, ...] | None = None
    required_worker_platform: str | None = None


@dataclass(frozen=True)
class PlannedCaptureTargetConstraint:
    capture_target: str
    provider_available: bool
    required_worker_platform: str | None = None
    availability_reason: str | None = None


@dataclass(frozen=True)
class _PullRequestBodyUpdateContext:
    github_client: object
    repo_full_name: str
    pr_number: int
    title: str
    base_branch: str
    body: str


def qa_demo_recording_enabled(effective_policy: dict[str, object] | None) -> bool:
    return bool((effective_policy or {}).get("qa_demo_recording_enabled"))


def qa_demo_max_attempts(settings) -> int:  # noqa: ANN001
    try:
        raw_value = getattr(settings, "qa_demo_max_attempts", 3)
        configured = 3 if raw_value is None else int(raw_value)
    except (TypeError, ValueError):
        configured = 3
    return max(1, configured)


def qa_demo_recorder_process_timeout_seconds(settings) -> float:  # noqa: ANN001
    try:
        raw_value = getattr(settings, "qa_demo_recorder_process_timeout_seconds", 900.0)
        configured = 900.0 if raw_value is None else float(raw_value)
    except (TypeError, ValueError):
        configured = 900.0
    return max(1.0, configured)


def storage_config_from_settings(settings) -> DemoArtifactStorageConfig:  # noqa: ANN001
    endpoint = str(getattr(settings, "qa_demo_artifact_endpoint", "") or "").strip()
    access_key = str(getattr(settings, "qa_demo_artifact_access_key", "") or "").strip()
    secret_key = str(getattr(settings, "qa_demo_artifact_secret_key", "") or "").strip()
    bucket = str(getattr(settings, "qa_demo_artifact_bucket", "") or "").strip()
    public_base_url = str(getattr(settings, "qa_demo_artifact_public_base_url", "") or "").strip().rstrip("/")
    secure = bool(getattr(settings, "qa_demo_artifact_secure", True))
    if not endpoint or not access_key or not secret_key or not bucket or not public_base_url:
        raise RuntimeError("QA demo artifact storage is not fully configured")
    return DemoArtifactStorageConfig(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        public_base_url=public_base_url,
        secure=secure,
    )


def resolve_preview_demo_url(release) -> str:  # noqa: ANN001
    urls = list(getattr(release, "service_urls", []) or [])
    for service_url in urls:
        if str(getattr(service_url, "service_kind", "") or "").strip() == "website" and str(
            getattr(service_url, "status", "") or ""
        ).strip() == "active":
            url = str(getattr(service_url, "url", "") or "").strip()
            if url:
                return url
    raise RuntimeError("QA demo recording requires an active preview website URL")


def _release_service_kinds(release) -> tuple[str, ...]:  # noqa: ANN001
    urls = list(getattr(release, "service_urls", []) or []) if release is not None else []
    ordered: list[str] = []
    for service_url in urls:
        kind = str(getattr(service_url, "service_kind", "") or "").strip()
        if kind and kind not in ordered:
            ordered.append(kind)
    return tuple(ordered)


def _release_service_urls_payload(
    release,  # noqa: ANN001
    *,
    include_recording_details: bool = False,
) -> list[dict[str, str]]:
    urls = list(getattr(release, "service_urls", []) or []) if release is not None else []
    payload: list[dict[str, str]] = []
    for service_url in urls:
        status = str(getattr(service_url, "status", "") or "").strip()
        url = str(getattr(service_url, "url", "") or "").strip()
        service_kind = str(getattr(service_url, "service_kind", "") or "").strip()
        if status != "active" or not url or service_kind not in {"website", "api"}:
            continue
        item = {
            "service_kind": service_kind,
            "service_name": str(getattr(service_url, "service_name", "") or "").strip(),
            "url": url,
        }
        if include_recording_details:
            recording_url, recording_headers = _release_service_recording_probe(service_url)
            if recording_url and recording_url != url:
                item["recording_url"] = recording_url
            recording_host_header = recording_headers.get("Host")
            if recording_host_header:
                item["recording_host_header"] = recording_host_header
        service_key = str(getattr(service_url, "service_key", "") or "").strip()
        if service_key:
            item["service_key"] = service_key
        payload.append(item)
    return payload


def release_context_sha256_for_service_urls(release_service_urls: list[dict[str, str]]) -> str:
    serialized = json.dumps(release_service_urls, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def release_context_sha256_for_release(*, release, release_service_urls: list[dict[str, str]]) -> str:  # noqa: ANN001
    commit_sha = str(getattr(release, "commit_sha", "") or "").strip()
    if not commit_sha:
        raise RuntimeError("QA demo recording requires a preview release commit SHA before proof can be recorded")
    payload = {
        "commit_sha": commit_sha,
        "service_urls": release_service_urls,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _release_readiness_failure_diagnostics(
    *,
    session,  # noqa: ANN001
    tenant,  # noqa: ANN001
    project,  # noqa: ANN001
    preview_release,  # noqa: ANN001
    release_service_urls: list[dict[str, str]],
) -> str:
    release_id = str(getattr(preview_release, "release_id", "") or "").strip()
    app_id = str(getattr(preview_release, "app_id", "") or "").strip() or None
    provider = str(getattr(preview_release, "provider", "") or "").strip()
    release_status = str(getattr(preview_release, "status", "") or "").strip()
    lines = [
        f"release_id: {release_id or '<missing>'}",
        f"release_status: {release_status or '<missing>'}",
        f"release_provider: {provider or '<missing>'}",
        "release_service_urls:",
        json.dumps(release_service_urls, sort_keys=True),
    ]
    if not release_id:
        lines.extend(
            [
                "provider_logs_status: unavailable",
                "provider_logs_error: preview release id is missing",
            ]
        )
        return "\n".join(lines).strip()
    try:
        logs = get_project_deployment_release_logs(
            session=session,
            tenant_id=str(getattr(tenant, "tenant_id", "") or "").strip(),
            project_id=str(getattr(project, "project_id", "") or "").strip(),
            release_id=release_id,
            app_id=app_id,
        )
    except Exception as exc:  # noqa: BLE001
        lines.extend(
            [
                "provider_logs_status: unavailable",
                f"provider_logs_error: {type(exc).__name__}: {exc}",
            ]
        )
        return "\n".join(lines).strip()
    provider_logs = str(getattr(logs, "logs", "") or "").strip()
    lines.extend(
        [
            "provider_logs_status: available",
            f"provider_deployment_uuid: {str(getattr(logs, 'deployment_uuid', '') or '').strip() or '<missing>'}",
            f"provider_application_uuid: {str(getattr(logs, 'application_uuid', '') or '').strip() or '<missing>'}",
            f"provider_release_status: {str(getattr(logs, 'status', '') or '').strip() or '<missing>'}",
            f"provider_logs_truncated: {str(bool(getattr(logs, 'truncated', False))).lower()}",
            "provider_logs:",
            provider_logs or "<empty>",
        ]
    )
    return "\n".join(lines).strip()


def _append_failure_diagnostics(*, failure_message: str, diagnostics: str) -> str:
    normalized_diagnostics = str(diagnostics or "").strip()
    if not normalized_diagnostics:
        return failure_message
    return f"{failure_message}\n\nRelease diagnostics:\n{normalized_diagnostics}"


def _ensure_failure_evidence_has_diagnostics(
    evidence: list[LocalQaFailureEvidence],
    *,
    failure_message: str,
) -> list[LocalQaFailureEvidence]:
    updated: list[LocalQaFailureEvidence] = []
    for item in evidence:
        if failure_message in item.error_message:
            updated.append(item)
        else:
            updated.append(replace(item, error_message=f"{item.error_message}\n\n{failure_message}".strip()))
    return updated


def _service_kind_from_item(item: object) -> str | None:
    if isinstance(item, dict):
        kind = str(item.get("kind") or item.get("service_kind") or "").strip()
        public = item.get("public", True)
    else:
        kind = str(getattr(item, "kind", "") or getattr(item, "service_kind", "") or "").strip()
        public = getattr(item, "public", True)
    if not kind or kind not in {"website", "api"}:
        return None
    if public is False:
        return None
    return kind


def _service_kinds_from_services(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    ordered: list[str] = []
    for item in value:
        kind = _service_kind_from_item(item)
        if kind and kind not in ordered:
            ordered.append(kind)
    return tuple(ordered)


def required_release_service_kinds(*, project, preview_release) -> tuple[str, ...]:  # noqa: ANN001
    release_snapshot = getattr(preview_release, "deployment_snapshot", None)
    if isinstance(release_snapshot, dict):
        snapshot_kinds = _service_kinds_from_services(release_snapshot.get("services"))
        if snapshot_kinds:
            return snapshot_kinds
    project_deployment_config = getattr(project, "deployment_config", None)
    if isinstance(project_deployment_config, dict):
        project_kinds = _service_kinds_from_services(project_deployment_config.get("services"))
        if project_kinds:
            return project_kinds
    return _release_service_kinds(preview_release)


def _parse_recorder_command(raw_value: object, *, provider_name: str) -> tuple[str, ...]:
    raw = str(raw_value or "").strip()
    if not raw:
        raise RuntimeError(f"QA demo {provider_name} recorder command is not configured")
    parts = tuple(shlex.split(raw))
    if not parts:
        raise RuntimeError(f"QA demo {provider_name} recorder command is invalid")
    return parts


def qa_demo_release_health_timeout_seconds(settings) -> float:  # noqa: ANN001
    try:
        raw_value = getattr(settings, "qa_demo_release_health_timeout_seconds", 10.0)
        configured = 10.0 if raw_value is None else float(raw_value)
    except (TypeError, ValueError):
        configured = 10.0
    return max(1.0, configured)


def qa_demo_artifact_url_timeout_seconds(settings) -> float:  # noqa: ANN001
    try:
        raw_value = getattr(settings, "qa_demo_artifact_url_timeout_seconds", 10.0)
        configured = 10.0 if raw_value is None else float(raw_value)
    except (TypeError, ValueError):
        configured = 10.0
    return max(1.0, configured)


def _default_service_url_probe(
    url: str,
    *,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
) -> int:
    request_headers = {"User-Agent": "MasterBuilder-QA-Demo/1.0"}
    request_headers.update(headers or {})
    request = urllib.request.Request(
        url,
        headers=request_headers,
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            return int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(str(exc)) from exc


def _default_artifact_url_probe(url: str, *, timeout_seconds: float) -> int:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MasterBuilder-QA-Demo/1.0",
            "Range": "bytes=0-0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            return int(getattr(response, "status", 200))
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(str(exc)) from exc


def ensure_artifact_url_reachable(
    artifact_url: str,
    *,
    artifact_url_probe: Callable[[str], int] | Callable[..., int] | None = None,
    timeout_seconds: float = 10.0,
) -> None:
    probe = artifact_url_probe or _default_artifact_url_probe
    try:
        status_code = int(probe(artifact_url, timeout_seconds=timeout_seconds))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"QA demo artifact URL is not reachable: {artifact_url}: {exc}") from exc
    if status_code >= 400:
        raise RuntimeError(f"QA demo artifact URL is not reachable: {artifact_url}: HTTP {status_code}")
    if status_code not in {200, 206}:
        raise RuntimeError(f"QA demo artifact URL did not return playable video evidence: {artifact_url}: HTTP {status_code}")


def ensure_release_ready_for_qa(
    release,  # noqa: ANN001
    *,
    required_service_kinds: tuple[str, ...],
    service_url_probe: Callable[[str], int] | Callable[..., int] | None = None,
    timeout_seconds: float = 10.0,
) -> None:
    required = tuple(dict.fromkeys(str(kind or "").strip() for kind in required_service_kinds if str(kind or "").strip()))
    if not required:
        return
    urls = list(getattr(release, "service_urls", []) or []) if release is not None else []
    inactive_failures: list[str] = []
    probe_failures: list[str] = []
    for required_kind in required:
        matching_urls = [
            service_url
            for service_url in urls
            if str(getattr(service_url, "service_kind", "") or "").strip() == required_kind
        ]
        active_service_urls = [
            service_url
            for service_url in matching_urls
            if str(getattr(service_url, "status", "") or "").strip() == "active"
            and str(getattr(service_url, "url", "") or "").strip()
        ]
        if not active_service_urls:
            inactive_failures.append(required_kind)
            continue
        if service_url_probe is None:
            continue
        for service_url in active_service_urls:
            public_url = str(getattr(service_url, "url", "") or "").strip()
            probe_url, probe_headers = _release_service_recording_probe(service_url)
            probe_label = public_url if probe_url == public_url else f"{public_url} via {probe_url}"
            try:
                if probe_headers:
                    status_code = int(
                        service_url_probe(
                            probe_url,
                            timeout_seconds=timeout_seconds,
                            headers=probe_headers,
                        )
                    )
                else:
                    status_code = int(service_url_probe(probe_url, timeout_seconds=timeout_seconds))
            except Exception as exc:  # noqa: BLE001
                probe_failures.append(f"{required_kind} ({probe_label}): {exc}")
                continue
            if not _release_service_status_is_ready(status_code):
                probe_failures.append(f"{required_kind} ({probe_label}): HTTP {status_code}")
    if inactive_failures:
        raise RuntimeError(
            "QA demo recording requires active release service URL(s); not active: "
            + ", ".join(sorted(inactive_failures))
        )
    if probe_failures:
        raise RuntimeError(
            "QA demo recording requires reachable release service URL(s); not reachable: "
            + "; ".join(sorted(probe_failures))
        )


def _release_service_status_is_ready(status_code: int) -> bool:
    return 200 <= status_code < 400 or status_code in _RELEASE_SERVICE_READY_STATUSES


def _release_service_recording_probe(service_url) -> tuple[str, dict[str, str]]:  # noqa: ANN001
    public_url = str(getattr(service_url, "url", "") or "").strip()
    internal_url = str(getattr(service_url, "internal_url", "") or "").strip()
    probe_url = _normalize_release_probe_url_for_current_runtime(internal_url or public_url)
    headers: dict[str, str] = {}
    host = str(getattr(service_url, "host", "") or "").strip()
    if internal_url and host:
        headers["Host"] = host
    return probe_url, headers


def _normalize_release_probe_url_for_current_runtime(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if parsed.hostname != "host.docker.internal" or Path("/.dockerenv").exists():
        return url
    netloc = "localhost"
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _is_builtin_android_recorder_command(command: tuple[str, ...] | None) -> bool:
    return any(str(part).endswith("qa_demo_android_recorder.py") for part in (command or ()))


def _is_builtin_ios_recorder_command(command: tuple[str, ...] | None) -> bool:
    return any(str(part).endswith("qa_demo_mobile_recorder.py") for part in (command or ()))


def _required_android_recorder_tool(*, env_var: str | None, default: str) -> str:
    if env_var is None:
        return default
    configured = str(os.environ.get(env_var) or "").strip()
    if configured:
        return configured
    discovered = _discover_android_sdk_build_tool(default)
    return discovered or default


def _discover_android_sdk_build_tool(tool_name: str) -> str | None:
    if shutil.which(tool_name) is not None:
        return tool_name
    sdk_roots = [
        str(os.environ.get("ANDROID_HOME") or "").strip(),
        str(os.environ.get("ANDROID_SDK_ROOT") or "").strip(),
        str(Path.home() / "Library" / "Android" / "sdk"),
        str(Path.home() / "Android" / "Sdk"),
    ]
    candidates: list[Path] = []
    for raw_root in sdk_roots:
        if not raw_root:
            continue
        build_tools_dir = Path(raw_root).expanduser() / "build-tools"
        if not build_tools_dir.exists():
            continue
        candidates.extend(path for path in build_tools_dir.glob(f"*/{tool_name}") if path.is_file())
    if not candidates:
        return None
    return str(sorted(candidates, key=_android_build_tool_version_key)[-1])


def _discover_android_emulator() -> str | None:
    path_emulator = shutil.which("emulator")
    if path_emulator is not None:
        return path_emulator
    sdk_roots = [
        str(os.environ.get("ANDROID_HOME") or "").strip(),
        str(os.environ.get("ANDROID_SDK_ROOT") or "").strip(),
        str(Path.home() / "Library" / "Android" / "sdk"),
        str(Path.home() / "Android" / "Sdk"),
    ]
    for raw_root in sdk_roots:
        if not raw_root:
            continue
        sdk_root = Path(raw_root).expanduser()
        for candidate in (sdk_root / "emulator" / "emulator", sdk_root / "tools" / "emulator"):
            if candidate.is_file():
                return str(candidate)
    return None


def _android_build_tool_version_key(path: Path) -> tuple[tuple[int, ...], str]:
    parts: list[int] = []
    for item in path.parent.name.split("."):
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    return tuple(parts), str(path)


def _ensure_worker_tool_available(*, command: str, purpose: str) -> None:
    if not command:
        raise RuntimeError(f"{purpose} command must not be blank")
    if shutil.which(command) is None:
        raise RuntimeError(f"{purpose} requires {command} on the worker PATH")


def _ready_adb_devices(adb_devices_output: str) -> tuple[str, ...]:
    devices: list[str] = []
    for raw_line in adb_devices_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return tuple(devices)


def _available_android_avds(emulator_list_avds_output: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in emulator_list_avds_output.splitlines() if line.strip())


def _ensure_builtin_android_runtime_ready() -> None:
    for tool in (
        _required_android_recorder_tool(env_var=None, default="adb"),
        _required_android_recorder_tool(env_var="QA_DEMO_ANDROID_AAPT", default="aapt"),
    ):
        _ensure_worker_tool_available(command=tool, purpose="Android QA demo recording")
    try:
        result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Android QA demo recording requires adb on the worker PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Android QA demo recording timed out while checking adb devices") from exc
    except subprocess.CalledProcessError as exc:
        stderr = str(exc.stderr or "").strip()
        stdout = str(exc.stdout or "").strip()
        details = stderr or stdout or f"exit code {exc.returncode}"
        raise RuntimeError(f"Android QA demo recording could not list adb devices: {details}") from exc
    if _ready_adb_devices(result.stdout):
        return
    emulator = _discover_android_emulator()
    if emulator is None:
        raise RuntimeError("No available Android emulator/device found via adb devices and Android emulator is unavailable")
    try:
        avd_result = subprocess.run(
            [emulator, "-list-avds"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Android QA demo recording timed out while checking Android Virtual Devices") from exc
    except subprocess.CalledProcessError as exc:
        stderr = str(exc.stderr or "").strip()
        details = f": {stderr}" if stderr else ""
        raise RuntimeError(f"Android QA demo recording could not list Android Virtual Devices{details}") from exc
    configured_avd = str(os.environ.get("QA_DEMO_ANDROID_AVD") or "").strip()
    avds = _available_android_avds(avd_result.stdout)
    if configured_avd and configured_avd not in avds:
        raise RuntimeError(f"Configured Android QA demo AVD does not exist: {configured_avd}")
    if not avds:
        raise RuntimeError("No available Android emulator/device found via adb devices and no Android Virtual Device exists")


def _ensure_builtin_ios_runtime_ready() -> None:
    try:
        result = subprocess.run(
            ["xcrun", "simctl", "list", "devices", "available"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("iOS QA demo recording requires xcrun on the worker PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("iOS QA demo recording timed out while checking available simulators") from exc
    except subprocess.CalledProcessError as exc:
        stderr = str(exc.stderr or "").strip()
        stdout = str(exc.stdout or "").strip()
        details = stderr or stdout or f"exit code {exc.returncode}"
        raise RuntimeError(f"iOS QA demo recording could not list available simulators: {details}") from exc
    try:
        preferred_simulator_udid(result.stdout)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _native_provider_runtime_status(
    *,
    capture_target: str,
    recorder_command: tuple[str, ...] | None,
) -> tuple[bool, str | None]:
    if capture_target == "ios" and _is_builtin_ios_recorder_command(recorder_command):
        try:
            _ensure_builtin_ios_runtime_ready()
        except RuntimeError as exc:
            return False, str(exc)
    if capture_target == "android" and _is_builtin_android_recorder_command(recorder_command):
        try:
            _ensure_builtin_android_runtime_ready()
        except RuntimeError as exc:
            return False, str(exc)
    return True, None


def ensure_capture_target_runtime_ready(capture_target: DemoCaptureTarget) -> None:
    ready, reason = _native_provider_runtime_status(
        capture_target=capture_target.capture_target,
        recorder_command=capture_target.recorder_command,
    )
    if not ready:
        raise RuntimeError(reason or f"QA demo capture target is not runtime-ready: {capture_target.capture_target}")


def _default_ios_recorder_command() -> tuple[str, ...] | None:
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "qa_demo_mobile_recorder.py"
    if not script_path.exists():
        return None
    return (sys.executable, str(script_path))


def _default_android_recorder_command() -> tuple[str, ...] | None:
    script_path = Path(__file__).resolve().parents[3] / "scripts" / "qa_demo_android_recorder.py"
    if not script_path.exists():
        return None
    return (sys.executable, str(script_path))


def _configured_worker_platform(*, raw_value: object, provider_name: str) -> str | None:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return None
    parsed = parse_worker_capability(normalized)
    if parsed is None:
        raise RuntimeError(
            f"QA demo {provider_name} worker platform is invalid: {normalized}. Allowed values: linux, macos"
    )
    return parsed.value


def _qa_demo_worker_platform_label(platform: str | None) -> str:
    if platform == "macos":
        return "macOS"
    if platform == "linux":
        return "Linux"
    return str(platform or "").strip() or "configured"


def resolve_available_capture_targets(*, settings, preview_release) -> dict[str, DemoCaptureTarget]:  # noqa: ANN001
    targets: dict[str, DemoCaptureTarget] = {}
    try:
        preview_url = resolve_preview_demo_url(preview_release)
    except RuntimeError:
        preview_url = ""
    if preview_url:
        targets["browser"] = DemoCaptureTarget(
            capture_target="browser",
            capture_reference=preview_url,
        )

    configured_ios_command = str(getattr(settings, "qa_demo_ios_recorder_command", "") or "").strip()
    ios_recorder_command = (
        _parse_recorder_command(configured_ios_command, provider_name="ios")
        if configured_ios_command
        else _default_ios_recorder_command()
    )
    if ios_recorder_command is not None:
        targets["ios"] = DemoCaptureTarget(
            capture_target="ios",
            capture_reference=(
                str(getattr(settings, "qa_demo_ios_capture_reference", "") or "").strip() or "ios-simulator://configured"
            ),
            recorder_command=ios_recorder_command,
            required_worker_platform="macos",
        )

    configured_android_command = str(getattr(settings, "qa_demo_android_recorder_command", "") or "").strip()
    android_recorder_command = (
        _parse_recorder_command(configured_android_command, provider_name="android")
        if configured_android_command
        else _default_android_recorder_command()
    )
    if android_recorder_command is not None:
        targets["android"] = DemoCaptureTarget(
            capture_target="android",
            capture_reference=(
                str(getattr(settings, "qa_demo_android_capture_reference", "") or "").strip()
                or "android-emulator://configured"
            ),
            recorder_command=android_recorder_command,
            required_worker_platform=(
                _configured_worker_platform(
                    raw_value=getattr(settings, "qa_demo_android_worker_platform", ""),
                    provider_name="android",
                )
                or "macos"
            ),
        )

    desktop_command = str(getattr(settings, "qa_demo_desktop_recorder_command", "") or "").strip()
    if desktop_command:
        targets["desktop"] = DemoCaptureTarget(
            capture_target="desktop",
            capture_reference=(
                str(getattr(settings, "qa_demo_desktop_capture_reference", "") or "").strip() or "desktop://configured"
            ),
            recorder_command=_parse_recorder_command(desktop_command, provider_name="desktop"),
            required_worker_platform=_configured_worker_platform(
                raw_value=getattr(settings, "qa_demo_desktop_worker_platform", ""),
                provider_name="desktop",
            ),
        )
    return targets


def planned_capture_target_constraints(*, settings) -> dict[str, PlannedCaptureTargetConstraint]:  # noqa: ANN001
    constraints: dict[str, PlannedCaptureTargetConstraint] = {
        "browser": PlannedCaptureTargetConstraint(
            capture_target="browser",
            provider_available=True,
            required_worker_platform=None,
            availability_reason="Browser capture uses the built-in Playwright recorder and requires a preview website URL at QA time.",
        )
    }
    configured_ios_command = str(getattr(settings, "qa_demo_ios_recorder_command", "") or "").strip()
    ios_recorder_command = (
        _parse_recorder_command(configured_ios_command, provider_name="ios")
        if configured_ios_command
        else _default_ios_recorder_command()
    )
    constraints["ios"] = PlannedCaptureTargetConstraint(
        capture_target="ios",
        provider_available=ios_recorder_command is not None,
        required_worker_platform="macos" if ios_recorder_command is not None else None,
        availability_reason=(
            "iOS capture requires the built-in or configured native recorder on a macOS worker. Runtime readiness is verified on the selected worker before recording."
            if ios_recorder_command is not None
            else "iOS capture provider is unavailable because no native iOS recorder command exists."
        ),
    )

    configured_android_command = str(getattr(settings, "qa_demo_android_recorder_command", "") or "").strip()
    android_recorder_command = (
        _parse_recorder_command(configured_android_command, provider_name="android")
        if configured_android_command
        else _default_android_recorder_command()
    )
    android_worker_platform = (
        _configured_worker_platform(
            raw_value=getattr(settings, "qa_demo_android_worker_platform", ""),
            provider_name="android",
        )
        or "macos"
        if android_recorder_command is not None
        else None
    )
    constraints["android"] = PlannedCaptureTargetConstraint(
        capture_target="android",
        provider_available=android_recorder_command is not None,
        required_worker_platform=android_worker_platform,
        availability_reason=(
            "Android capture requires the built-in or configured native recorder on a "
            f"{_qa_demo_worker_platform_label(android_worker_platform)} Android worker. Runtime readiness is verified on the selected worker before recording."
            if android_recorder_command is not None
            else "Android capture provider is unavailable because no native Android recorder command exists."
        ),
    )

    desktop_command = str(getattr(settings, "qa_demo_desktop_recorder_command", "") or "").strip()
    desktop_available = bool(desktop_command)
    constraints["desktop"] = PlannedCaptureTargetConstraint(
        capture_target="desktop",
        provider_available=desktop_available,
        required_worker_platform=(
            _configured_worker_platform(
                raw_value=getattr(settings, "qa_demo_desktop_worker_platform", ""),
                provider_name="desktop",
            )
            if desktop_available
            else None
        ),
        availability_reason=(
            "Desktop capture uses the configured desktop recorder command."
            if desktop_available
            else "Desktop capture provider is unavailable because no desktop recorder command is configured."
        ),
    )
    return constraints


def planned_capture_target_constraints_payload(*, settings) -> list[dict[str, str | bool | None]]:  # noqa: ANN001
    payload: list[dict[str, str | bool | None]] = []
    for constraint in planned_capture_target_constraints(settings=settings).values():
        payload.append(
            {
                "capture_target": constraint.capture_target,
                "provider_available": constraint.provider_available,
                "required_worker_platform": constraint.required_worker_platform,
                "availability_reason": constraint.availability_reason,
            }
        )
    return payload


def _available_capture_targets_payload(
    targets: dict[str, DemoCaptureTarget],
    *,
    source_paths_by_target: dict[str, tuple[str, ...]] | None = None,
    release_service_urls: list[dict[str, str]] | None = None,
    release_commit_sha: str = "",
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    release_service_urls = list(release_service_urls or [])
    release_commit_sha = str(release_commit_sha or "").strip()
    for target in targets.values():
        payload.append(
            {
                "capture_target": target.capture_target,
                "capture_reference": target.capture_reference,
                "source_paths": list((source_paths_by_target or {}).get(target.capture_target, ())),
                "release_commit_sha": release_commit_sha,
                "release_service_urls": release_service_urls,
                "release_api_base_url": _first_release_service_url(release_service_urls, service_kind="api"),
                "release_browser_url": _first_release_service_url(release_service_urls, service_kind="website"),
            }
        )
    return payload


def required_capture_targets(plan: PmPlan) -> tuple[str, ...]:
    ordered: list[str] = []
    for requirement in plan.demo_requirements:
        if requirement.capture_target not in ordered:
            ordered.append(requirement.capture_target)
    return tuple(ordered)


def ensure_pm_demo_requirements_are_recordable(plan: PmPlan) -> None:
    if not plan.demo_requirements:
        raise RuntimeError("QA demo recording requires PM demo requirements with explicit capture targets")
    insufficient_variants = [
        f"{requirement.capture_target}: {requirement.title}"
        for requirement in plan.demo_requirements
        if len(list(requirement.variants or [])) < _MIN_DEMO_REQUIREMENT_VARIANTS
    ]
    if insufficient_variants:
        raise RuntimeError(
            "QA demo recording requires at least two PM demo requirement variants for QA walkthrough coverage: "
            + "; ".join(insufficient_variants)
        )


def ensure_pm_demo_requirements_cover_project_targets(*, plan: PmPlan, request: WorkflowRequest) -> None:
    selected_targets = set(required_capture_targets(plan))
    required_project_targets = {
        target
        for target in request.project_demo_capture_targets
        if target in _DEMO_CAPTURE_TARGETS
    }
    missing_targets = sorted(required_project_targets - selected_targets)
    if missing_targets:
        raise RuntimeError(
            "QA demo recording PM requirements are missing required project demo capture target(s): "
            + ", ".join(missing_targets)
        )


def required_recording_counts_by_target(plan: PmPlan) -> dict[str, int]:
    required_counts: dict[str, int] = {}
    for requirement in plan.demo_requirements:
        required_counts[requirement.capture_target] = (
            required_counts.get(requirement.capture_target, 0) + 1 + len(requirement.variants or [])
        )
    return required_counts


def browser_capture_required(plan: PmPlan) -> bool:
    return "browser" in required_capture_targets(plan)


def recorded_capture_targets(recordings: list[QaRecording] | tuple[QaRecording, ...]) -> tuple[str, ...]:
    ordered: list[str] = []
    for recording in recordings:
        capture_target = str(recording.capture_target or "").strip()
        if capture_target and capture_target not in ordered:
            ordered.append(capture_target)
    return tuple(ordered)


def _normalize_required_capture_targets(required_capture_targets: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    ordered: list[str] = []
    for target in required_capture_targets or ():
        normalized = str(target or "").strip()
        if not normalized:
            continue
        if normalized not in _DEMO_CAPTURE_TARGETS:
            raise ValueError(f"Unsupported QA demo capture target: {normalized}")
        if normalized not in ordered:
            ordered.append(normalized)
    return tuple(ordered)


def _normalize_required_recording_counts(required_recording_counts: dict[str, int] | None) -> dict[str, int]:
    normalized_counts: dict[str, int] = {}
    for target, count in (required_recording_counts or {}).items():
        normalized_target = str(target or "").strip()
        if not normalized_target:
            continue
        if normalized_target not in _DEMO_CAPTURE_TARGETS:
            raise ValueError(f"Unsupported QA demo capture target: {normalized_target}")
        normalized_count = int(count)
        if normalized_count <= 0:
            raise ValueError(f"Required QA demo recording count must be positive for target: {normalized_target}")
        normalized_counts[normalized_target] = normalized_count
    return normalized_counts


def _require_demo_evidence_field(value: object, *, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or any(char in normalized for char in "\r\n"):
        raise RuntimeError(f"QA demo evidence recording metadata is not serializable: {field}")
    return normalized


def _require_demo_evidence_line_field(value: object, *, field: str) -> str:
    normalized = _require_demo_evidence_field(value, field=field)
    if "[" in normalized or "]" in normalized:
        raise RuntimeError(f"QA demo evidence recording metadata is not serializable: {field}")
    return normalized


def _require_demo_evidence_capture_target(value: object) -> str:
    normalized = _require_demo_evidence_line_field(value, field="capture_target")
    if normalized not in _DEMO_CAPTURE_TARGETS:
        raise ValueError(f"Unsupported QA demo capture target: {normalized}")
    return normalized


def _validate_demo_evidence_recording_metadata(recordings: list[QaRecording]) -> None:
    for recording in recordings:
        _require_demo_evidence_line_field(recording.name, field="name")
        _require_demo_evidence_capture_target(recording.capture_target)
        _require_demo_evidence_line_field(recording.capture_reference, field="capture_reference")
        _require_demo_evidence_line_field(recording.object_key, field="object_key")
        _require_demo_evidence_content_sha256(recording)
        _require_demo_evidence_release_commit_sha(recording)
        _require_demo_evidence_release_context_sha256(recording)
        _require_demo_evidence_field(recording.artifact_url, field="artifact_url")
    _validate_recordings_share_release_context(recordings)


def _validate_demo_failure_evidence_metadata(failure_evidence: list[QaFailureEvidence]) -> None:
    for item in failure_evidence:
        _require_demo_evidence_line_field(item.name, field="name")
        _require_demo_evidence_capture_target(item.capture_target)
        _require_demo_evidence_line_field(item.capture_reference, field="capture_reference")
        _require_demo_evidence_line_field(item.object_key, field="object_key")
        _require_demo_evidence_content_sha256(item)
        _require_demo_evidence_release_commit_sha(item)
        _require_demo_evidence_release_context_sha256(item)
        _require_demo_evidence_field(item.artifact_url, field="artifact_url")
        _require_demo_failure_error_message(item.error_message)
    _validate_failure_evidence_share_release_context(failure_evidence)


def _require_demo_failure_error_message(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise RuntimeError("QA demo failure evidence error message is required before PR evidence")
    return normalized


def _require_demo_evidence_content_sha256(recording: object) -> str:
    value = str(getattr(recording, "content_sha256", "") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("QA demo recording content sha256 is required before PR evidence")
    return value


def _require_demo_evidence_release_context_sha256(recording: object) -> str:
    value = str(getattr(recording, "release_context_sha256", "") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise RuntimeError("QA demo recording release context sha256 is required before PR evidence")
    return value


def _require_demo_evidence_release_commit_sha(recording: object) -> str:
    value = str(getattr(recording, "release_commit_sha", "") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", value):
        raise RuntimeError("QA demo recording release commit sha is required before PR evidence")
    return value


def _validate_recordings_share_release_context(recordings: list[QaRecording]) -> None:
    release_contexts = {
        str(getattr(recording, "release_context_sha256", "") or "").strip().lower()
        for recording in recordings
    }
    if len(release_contexts) > 1:
        raise RuntimeError("QA demo recordings must all prove the same release context before PR evidence")
    release_commits = {
        str(getattr(recording, "release_commit_sha", "") or "").strip().lower()
        for recording in recordings
    }
    if len(release_commits) > 1:
        raise RuntimeError("QA demo recordings must all prove the same release commit before PR evidence")


def _validate_failure_evidence_share_release_context(failure_evidence: list[QaFailureEvidence]) -> None:
    release_contexts = {
        str(getattr(item, "release_context_sha256", "") or "").strip().lower()
        for item in failure_evidence
    }
    if len(release_contexts) > 1:
        raise RuntimeError("QA demo failure evidence must all prove the same release context before PR evidence")
    release_commits = {
        str(getattr(item, "release_commit_sha", "") or "").strip().lower()
        for item in failure_evidence
    }
    if len(release_commits) > 1:
        raise RuntimeError("QA demo failure evidence must all prove the same release commit before PR evidence")


def _validate_recordings_match_release_context(
    *,
    recordings: list[QaRecording],
    expected_release_context_sha256: str,
) -> None:
    expected = _require_content_sha256_value(expected_release_context_sha256)
    mismatched = [
        str(getattr(recording, "name", "") or "").strip() or "<unnamed>"
        for recording in recordings
        if str(getattr(recording, "release_context_sha256", "") or "").strip().lower() != expected
    ]
    if mismatched:
        raise RuntimeError(
            "QA demo recording release context does not match current release before reuse: "
            + ", ".join(mismatched)
        )


def _validate_recordings_match_release_commit(
    *,
    recordings: list[QaRecording],
    expected_release_commit_sha: str,
) -> None:
    expected = _require_release_commit_sha_value(expected_release_commit_sha)
    mismatched = [
        str(getattr(recording, "name", "") or "").strip() or "<unnamed>"
        for recording in recordings
        if str(getattr(recording, "release_commit_sha", "") or "").strip().lower() != expected
    ]
    if mismatched:
        raise RuntimeError(
            "QA demo recording release commit does not match current release before reuse: "
            + ", ".join(mismatched)
        )


def _validate_distinct_demo_recording_artifacts(recordings: list[QaRecording]) -> None:
    object_key_counts = Counter(str(recording.object_key or "").strip() for recording in recordings)
    duplicate_object_keys = [object_key for object_key, count in object_key_counts.items() if object_key and count > 1]
    if duplicate_object_keys:
        raise RuntimeError(
            "QA demo recording object keys must be unique before PR evidence: " + ", ".join(duplicate_object_keys)
        )
    artifact_url_counts = Counter(str(recording.artifact_url or "").strip() for recording in recordings)
    duplicate_artifact_urls = [url for url, count in artifact_url_counts.items() if url and count > 1]
    if duplicate_artifact_urls:
        raise RuntimeError(
            "QA demo recording artifact URLs must be unique before PR evidence: " + ", ".join(duplicate_artifact_urls)
        )
    content_sha256_counts = Counter(str(getattr(recording, "content_sha256", "") or "").strip().lower() for recording in recordings)
    duplicate_content_sha256 = [
        content_sha256 for content_sha256, count in content_sha256_counts.items() if content_sha256 and count > 1
    ]
    if duplicate_content_sha256:
        raise RuntimeError(
            "QA demo recording content sha256 values must be unique before PR evidence: "
            + ", ".join(duplicate_content_sha256)
        )


def _validate_distinct_demo_failure_evidence_artifacts(failure_evidence: list[QaFailureEvidence]) -> None:
    object_key_counts = Counter(str(item.object_key or "").strip() for item in failure_evidence)
    duplicate_object_keys = [object_key for object_key, count in object_key_counts.items() if object_key and count > 1]
    if duplicate_object_keys:
        raise RuntimeError(
            "QA demo failure evidence object keys must be unique before PR evidence: " + ", ".join(duplicate_object_keys)
        )
    artifact_url_counts = Counter(str(item.artifact_url or "").strip() for item in failure_evidence)
    duplicate_artifact_urls = [url for url, count in artifact_url_counts.items() if url and count > 1]
    if duplicate_artifact_urls:
        raise RuntimeError(
            "QA demo failure evidence artifact URLs must be unique before PR evidence: " + ", ".join(duplicate_artifact_urls)
        )
    content_sha256_counts = Counter(
        str(getattr(item, "content_sha256", "") or "").strip().lower() for item in failure_evidence
    )
    duplicate_content_sha256 = [
        content_sha256 for content_sha256, count in content_sha256_counts.items() if content_sha256 and count > 1
    ]
    if duplicate_content_sha256:
        raise RuntimeError(
            "QA demo failure evidence content sha256 values must be unique before PR evidence: "
            + ", ".join(duplicate_content_sha256)
        )


def remaining_capture_targets(plan: PmPlan, recordings: list[QaRecording] | tuple[QaRecording, ...]) -> tuple[str, ...]:
    recorded_counts: dict[str, int] = {}
    for recording in recordings:
        capture_target = str(recording.capture_target or "").strip()
        if capture_target:
            recorded_counts[capture_target] = recorded_counts.get(capture_target, 0) + 1
    required_counts = required_recording_counts_by_target(plan)
    return tuple(
        target
        for target in required_capture_targets(plan)
        if recorded_counts.get(target, 0) < required_counts.get(target, 0)
    )


def next_required_qa_demo_worker_capability(
    *,
    settings,  # noqa: ANN001
    plan: PmPlan,
    recordings: list[QaRecording] | tuple[QaRecording, ...],
):
    constraints = planned_capture_target_constraints(settings=settings)
    for target_name in remaining_capture_targets(plan, recordings):
        required_platform = constraints.get(target_name).required_worker_platform if target_name in constraints else None
        parsed = parse_worker_capability(required_platform)
        if parsed is not None:
            return parsed
    return None


def _capture_target_runs_on_worker(*, capture_target: DemoCaptureTarget, request: WorkflowRequest) -> bool:
    required_platform = str(capture_target.required_worker_platform or "").strip()
    if not required_platform:
        return True
    return request.current_worker_capability.value == required_platform


def build_demo_evidence_section(
    recordings: list[QaRecording],
    *,
    required_capture_targets: list[str] | tuple[str, ...] | None = None,
    required_recording_counts: dict[str, int] | None = None,
) -> str:
    lines = [DEMO_EVIDENCE_HEADING, DEMO_EVIDENCE_MARKER]
    required_targets = _normalize_required_capture_targets(required_capture_targets)
    if required_targets:
        lines.append(f"{DEMO_EVIDENCE_REQUIRED_TARGETS_MARKER} {','.join(required_targets)} -->")
    normalized_counts = _normalize_required_recording_counts(required_recording_counts)
    if normalized_counts:
        serialized_counts = ",".join(f"{target}={count}" for target, count in normalized_counts.items())
        lines.append(f"{DEMO_EVIDENCE_REQUIRED_COUNTS_MARKER} {serialized_counts} -->")
    for recording in recordings:
        name = _require_demo_evidence_line_field(recording.name, field="name")
        capture_target = _require_demo_evidence_capture_target(recording.capture_target)
        capture_reference = _require_demo_evidence_line_field(recording.capture_reference, field="capture_reference")
        object_key = _require_demo_evidence_line_field(recording.object_key, field="object_key")
        content_sha256 = _require_demo_evidence_content_sha256(recording)
        release_commit_sha = _require_demo_evidence_release_commit_sha(recording)
        release_context_sha256 = _require_demo_evidence_release_context_sha256(recording)
        artifact_url = _require_demo_evidence_field(recording.artifact_url, field="artifact_url")
        lines.append(
            f"- {name} "
            f"[target={capture_target}; reference={capture_reference}; object_key={object_key}; "
            f"sha256={content_sha256}; release_commit_sha={release_commit_sha}; "
            f"release_context_sha256={release_context_sha256}]: "
            f"{artifact_url}"
        )
    return "\n".join(lines).strip()


def build_demo_failure_evidence_section(failure_evidence: list[QaFailureEvidence]) -> str:
    lines = [DEMO_FAILURE_EVIDENCE_HEADING, DEMO_FAILURE_EVIDENCE_MARKER]
    for item in failure_evidence:
        name = _require_demo_evidence_line_field(item.name, field="name")
        capture_target = _require_demo_evidence_capture_target(item.capture_target)
        capture_reference = _require_demo_evidence_line_field(item.capture_reference, field="capture_reference")
        object_key = _require_demo_evidence_line_field(item.object_key, field="object_key")
        content_sha256 = _require_demo_evidence_content_sha256(item)
        release_commit_sha = _require_demo_evidence_release_commit_sha(item)
        release_context_sha256 = _require_demo_evidence_release_context_sha256(item)
        artifact_url = _require_demo_evidence_field(item.artifact_url, field="artifact_url")
        error_message = _require_demo_failure_error_message(item.error_message)
        lines.append(
            f"- {name} "
            f"[target={capture_target}; reference={capture_reference}; object_key={object_key}; "
            f"sha256={content_sha256}; release_commit_sha={release_commit_sha}; "
            f"release_context_sha256={release_context_sha256}]: "
            f"{artifact_url}"
        )
        lines.append(f"  Error: {json.dumps(error_message, ensure_ascii=True)}")
    return "\n".join(lines).strip()


def upsert_demo_evidence_section(
    *,
    body: str | None,
    recordings: list[QaRecording],
    required_capture_targets: list[str] | tuple[str, ...] | None = None,
    required_recording_counts: dict[str, int] | None = None,
) -> str:
    evidence = build_demo_evidence_section(
        recordings,
        required_capture_targets=required_capture_targets,
        required_recording_counts=required_recording_counts,
    )
    normalized_body = str(body or "").strip()
    if not normalized_body:
        return evidence
    pattern = re.compile(r"^## Demo Evidence\s*$.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    if pattern.search(normalized_body):
        return pattern.sub(evidence + "\n\n", normalized_body).strip()
    return f"{normalized_body}\n\n{evidence}".strip()


def upsert_demo_failure_evidence_section(
    *,
    body: str | None,
    failure_evidence: list[QaFailureEvidence],
) -> str:
    evidence = build_demo_failure_evidence_section(failure_evidence)
    normalized_body = str(body or "").strip()
    if not normalized_body:
        return evidence
    pattern = re.compile(r"^## Demo Failure Evidence\s*$.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL)
    if pattern.search(normalized_body):
        return pattern.sub(evidence + "\n\n", normalized_body).strip()
    return f"{normalized_body}\n\n{evidence}".strip()


def _content_type_for_recording(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".mov":
        return "video/quicktime"
    raise RuntimeError(f"Unsupported QA demo recording file type: {suffix or '<none>'}")


def _content_type_for_failure_evidence(path: Path) -> str:
    if path.suffix.lower() == ".txt":
        return "text/plain"
    return _content_type_for_recording(path)


def _validate_local_recording_file(path: Path) -> None:
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"QA demo recording file is missing: {path}")
    payload = path.read_bytes()
    if len(payload) < 1024:
        raise RuntimeError(f"QA demo recording file is too small to be valid video evidence: {path}")
    suffix = path.suffix.lower()
    if suffix == ".webm":
        if not payload.startswith(b"\x1a\x45\xdf\xa3"):
            raise RuntimeError(f"QA demo recording file is not valid video evidence: {path}")
        return
    if suffix in {".mp4", ".mov"}:
        header = payload[:64]
        if b"ftyp" not in header or b"moov" not in payload:
            raise RuntimeError(f"QA demo recording file is not valid video evidence: {path}")
        return
    raise RuntimeError(f"Unsupported QA demo recording file type: {suffix or '<none>'}")


def _validate_local_failure_evidence_file(path: Path) -> None:
    if path.suffix.lower() != ".txt":
        _validate_local_recording_file(path)
        return
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"QA demo failure evidence file is missing: {path}")
    payload = path.read_bytes()
    if not payload.strip():
        raise RuntimeError(f"QA demo failure evidence file is empty: {path}")
    try:
        payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"QA demo failure evidence file is not valid UTF-8 text: {path}") from exc


def _parse_recorder_output(
    *,
    output_path: Path,
    output_dir: Path,
    capture_target: DemoCaptureTarget,
) -> list[LocalQaRecording]:
    result = json.loads(output_path.read_text(encoding="utf-8"))
    recordings: list[LocalQaRecording] = []
    seen_paths: set[str] = set()
    resolved_output_dir = output_dir.resolve()
    for item in list(result.get("recordings") or []):
        if not isinstance(item, dict):
            raise RuntimeError("QA demo recorder returned invalid recordings payload")
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if not name or not path:
            raise RuntimeError("QA demo recorder returned incomplete recording metadata")
        reported_capture_target = str(item.get("capture_target") or "").strip()
        if reported_capture_target and reported_capture_target != capture_target.capture_target:
            raise RuntimeError(
                "QA demo recorder returned capture target outside planned target: "
                f"{reported_capture_target} != {capture_target.capture_target}"
            )
        reported_capture_reference = str(item.get("capture_reference") or "").strip()
        if reported_capture_reference and reported_capture_reference != capture_target.capture_reference:
            raise RuntimeError(
                "QA demo recorder returned capture reference outside planned target: "
                f"{reported_capture_reference} != {capture_target.capture_reference}"
            )
        source = Path(path).resolve()
        source_key = str(source)
        if source_key in seen_paths:
            raise RuntimeError(f"QA demo recorder returned duplicate local recording path: {source}")
        seen_paths.add(source_key)
        try:
            source.relative_to(resolved_output_dir)
        except ValueError as exc:
            raise RuntimeError(
                f"QA demo recorder returned recording path outside recorder output directory: {source}"
            ) from exc
        _validate_local_recording_file(source)
        content_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        recordings.append(
            LocalQaRecording(
                name=name,
                path=str(source),
                capture_target=capture_target.capture_target,
                capture_reference=capture_target.capture_reference,
                content_type=_content_type_for_recording(source),
                content_sha256=content_sha256,
            )
        )
    if not recordings:
        raise RuntimeError("QA demo recorder produced no recordings")
    return recordings


def _parse_recorder_failure_evidence(
    *,
    output_path: Path,
    output_dir: Path,
    capture_target: DemoCaptureTarget,
) -> list[LocalQaFailureEvidence]:
    if not output_path.exists():
        return []
    result = json.loads(output_path.read_text(encoding="utf-8"))
    evidence: list[LocalQaFailureEvidence] = []
    seen_paths: set[str] = set()
    resolved_output_dir = output_dir.resolve()
    for item in list(result.get("failure_evidence") or []):
        if not isinstance(item, dict):
            raise RuntimeError("QA demo recorder returned invalid failure evidence payload")
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        error_message = str(item.get("error_message") or "").strip()
        if not name or not path or not error_message:
            raise RuntimeError("QA demo recorder returned incomplete failure evidence metadata")
        reported_capture_target = str(item.get("capture_target") or "").strip()
        if reported_capture_target and reported_capture_target != capture_target.capture_target:
            raise RuntimeError(
                "QA demo recorder returned failure evidence outside planned target: "
                f"{reported_capture_target} != {capture_target.capture_target}"
            )
        reported_capture_reference = str(item.get("capture_reference") or "").strip()
        if reported_capture_reference and reported_capture_reference != capture_target.capture_reference:
            raise RuntimeError(
                "QA demo recorder returned failure evidence reference outside planned target: "
                f"{reported_capture_reference} != {capture_target.capture_reference}"
            )
        source = Path(path).resolve()
        source_key = str(source)
        if source_key in seen_paths:
            raise RuntimeError(f"QA demo recorder returned duplicate local failure evidence path: {source}")
        seen_paths.add(source_key)
        try:
            source.relative_to(resolved_output_dir)
        except ValueError as exc:
            raise RuntimeError(
                f"QA demo recorder returned failure evidence path outside recorder output directory: {source}"
            ) from exc
        _validate_local_failure_evidence_file(source)
        content_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
        evidence.append(
            LocalQaFailureEvidence(
                name=name,
                path=str(source),
                capture_target=capture_target.capture_target,
                capture_reference=capture_target.capture_reference,
                content_type=_content_type_for_failure_evidence(source),
                content_sha256=content_sha256,
                error_message=error_message,
            )
        )
    return evidence


def _invoke_json_recorder(
    *,
    command: list[str],
    payload: dict[str, object],
    env: dict[str, str] | None,
    capture_target: DemoCaptureTarget,
    request: WorkflowRequest,
    timeout_seconds: float,
) -> list[LocalQaRecording]:
    with TemporaryDirectory(prefix=f"qa-demo-{capture_target.capture_target}-") as tmp_dir:
        input_path = Path(tmp_dir) / "input.json"
        output_path = Path(tmp_dir) / "output.json"
        output_dir = Path(tmp_dir) / "videos"
        payload["output_dir"] = str(output_dir)
        input_path.write_text(json.dumps(payload), encoding="utf-8")
        try:
            completed = subprocess.run(
                [*command, str(input_path), str(output_path)],
                check=False,
                text=True,
                capture_output=True,
                env=env,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"QA demo recorder command timed out after {timeout_seconds:.1f} seconds: {' '.join(command)}"
            ) from exc
        except subprocess.CalledProcessError as exc:
            stderr = str(exc.stderr or "").strip()
            stdout = str(exc.stdout or "").strip()
            details = stderr or stdout
            if details:
                raise RuntimeError(
                    f"QA demo recorder command failed ({exc.returncode}): {' '.join(command)}\n{details}"
                ) from exc
            raise RuntimeError(
                f"QA demo recorder command failed ({exc.returncode}): {' '.join(command)}"
            ) from exc
        returncode = int(getattr(completed, "returncode", 0) or 0)
        if returncode != 0:
            stderr = str(completed.stderr or "").strip()
            stdout = str(completed.stdout or "").strip()
            details = stderr or stdout
            failure_evidence = _parse_recorder_failure_evidence(
                output_path=output_path,
                output_dir=output_dir,
                capture_target=capture_target,
            )
            if details:
                message = f"QA demo recorder command failed ({returncode}): {' '.join(command)}\n{details}"
            else:
                message = f"QA demo recorder command failed ({returncode}): {' '.join(command)}"
            raise QaDemoRecordingFailure(
                message,
                failure_evidence=_copy_failure_evidence(failure_evidence, request=request),
            )
        recordings = _parse_recorder_output(
            output_path=output_path,
            output_dir=output_dir,
            capture_target=capture_target,
        )
        return _copy_recordings(recordings, request=request)


def _canonical_native_selector(selector: str | None) -> str | None:
    raw = str(selector or "").strip()
    if not raw:
        return None
    if raw.startswith("id=") or raw.startswith("text="):
        return raw
    if re.fullmatch(r"[a-z0-9_]+", raw):
        return f"id={raw}"
    return f"text={raw}"


def _normalize_native_step(step: QaStep) -> QaStep:
    selector = _canonical_native_selector(step.selector)
    if step.action == "wait_for_text" and not str(step.value or "").strip() and selector:
        if selector.startswith("text="):
            return replace(step, selector=None, value=selector.removeprefix("text="))
        return replace(step, selector=None, value=selector)
    return replace(step, selector=selector)


def _normalize_native_scenario(scenario: QaScenario) -> QaScenario:
    return replace(
        scenario,
        steps=[_normalize_native_step(step) for step in scenario.steps],
    )


def _validate_native_scenarios(qa_result: QaResult) -> QaResult:
    normalized_scenarios: list[QaScenario] = []
    for scenario in qa_result.scenarios:
        if scenario.capture_target not in _NATIVE_CAPTURE_TARGETS:
            normalized_scenarios.append(scenario)
            continue
        normalized = _normalize_native_scenario(scenario)
        for step in normalized.steps:
            if step.selector in _TRANSIENT_NATIVE_SELECTORS and step.action in {"assert_visible", "assert_text", "click"}:
                raise RuntimeError(
                    "QA demo native scenarios must not use splash_screen as a required assertion or click target"
                )
        normalized_scenarios.append(normalized)
    return replace(qa_result, scenarios=normalized_scenarios)


def _validate_executable_qa_scenarios(qa_result: QaResult) -> None:
    for scenario in qa_result.scenarios:
        scenario_name = str(scenario.name or "").strip() or "<unnamed>"
        capture_target = str(scenario.capture_target or "").strip() or "<unknown>"
        if not scenario.steps:
            raise RuntimeError(
                f"QA demo scenario '{scenario_name}' for {capture_target} requires executable steps"
            )
        if not any(step.action in _QA_PROOF_STEP_ACTIONS for step in scenario.steps):
            raise RuntimeError(
                f"QA demo scenario '{scenario_name}' for {capture_target} requires at least one proof assertion step"
            )


def _recording_scenario_key(*, name: str, capture_target: str) -> tuple[str, str]:
    return (str(capture_target or "").strip(), str(name or "").strip())


def _validate_recorded_scenario_proof(qa_result: QaResult) -> QaResult:
    if not qa_result.recordings:
        return qa_result
    normalized_result = _validate_native_scenarios(qa_result)
    scenario_counts = Counter(
        _recording_scenario_key(name=scenario.name, capture_target=scenario.capture_target)
        for scenario in normalized_result.scenarios
    )
    duplicate_scenarios = [
        f"{capture_target}: {name}" for (capture_target, name), count in scenario_counts.items() if count > 1
    ]
    if duplicate_scenarios:
        raise RuntimeError(
            "QA demo scenario proof keys must be unique before PR evidence: " + ", ".join(duplicate_scenarios)
        )
    recording_counts = Counter(
        _recording_scenario_key(name=recording.name, capture_target=recording.capture_target)
        for recording in normalized_result.recordings
    )
    duplicate_recordings = [
        f"{capture_target}: {name}" for (capture_target, name), count in recording_counts.items() if count > 1
    ]
    if duplicate_recordings:
        raise RuntimeError(
            "QA demo recording proof keys must be unique before PR evidence: " + ", ".join(duplicate_recordings)
        )
    scenarios_by_key = {
        _recording_scenario_key(name=scenario.name, capture_target=scenario.capture_target): scenario
        for scenario in normalized_result.scenarios
    }
    recording_keys = set(recording_counts)
    missing = [
        f"{recording.capture_target}: {recording.name}"
        for recording in normalized_result.recordings
        if _recording_scenario_key(name=recording.name, capture_target=recording.capture_target) not in scenarios_by_key
    ]
    if missing:
        raise RuntimeError(
            "QA demo recording proof is missing matching executable scenario(s): " + ", ".join(missing)
        )
    recorded_scenarios = [
        scenario
        for scenario in normalized_result.scenarios
        if _recording_scenario_key(name=scenario.name, capture_target=scenario.capture_target)
        in recording_keys
    ]
    _validate_executable_qa_scenarios(replace(normalized_result, scenarios=recorded_scenarios))
    return normalized_result


def _validate_browser_scenarios_stay_on_preview_origin(
    *,
    scenarios: list[QaScenario],
    capture_reference: str,
) -> None:
    parsed_preview = urllib.parse.urlparse(str(capture_reference or "").strip())
    if not parsed_preview.scheme or not parsed_preview.netloc:
        raise RuntimeError("QA demo browser capture reference must be an absolute preview release URL")
    preview_origin = (parsed_preview.scheme, parsed_preview.netloc)
    for scenario in scenarios:
        _validate_browser_url_value(
            value=scenario.start_path,
            preview_url=capture_reference,
            preview_origin=preview_origin,
            context=f"start_path for scenario '{scenario.name}'",
        )
        for step in scenario.steps:
            if step.action not in {"goto", "wait_for_url"}:
                continue
            _validate_browser_url_value(
                value=step.value,
                preview_url=capture_reference,
                preview_origin=preview_origin,
                context=f"{step.action} in scenario '{scenario.name}'",
            )


def _validate_browser_url_value(
    *,
    value: str | None,
    preview_url: str,
    preview_origin: tuple[str, str],
    context: str,
) -> None:
    raw = str(value or "/").strip() or "/"
    parsed = urllib.parse.urlparse(urllib.parse.urljoin(preview_url, raw))
    if (parsed.scheme, parsed.netloc) != preview_origin:
        raise RuntimeError(
            "QA demo browser scenario must stay on preview release origin: "
            f"{context} resolved to {parsed.scheme}://{parsed.netloc}"
        )


def _validate_recordings_cover_required_targets(
    *,
    recordings: list[QaRecording],
    required_capture_targets: list[str] | tuple[str, ...] | None,
) -> None:
    required_targets = _normalize_required_capture_targets(required_capture_targets)
    if not required_targets:
        return
    recorded_targets = set(recorded_capture_targets(recordings))
    missing_targets = [target for target in required_targets if target not in recorded_targets]
    if missing_targets:
        raise RuntimeError(
            "QA demo evidence is missing required capture target(s): " + ", ".join(missing_targets)
        )


def _validate_recordings_cover_required_counts(
    *,
    recordings: list[QaRecording],
    required_recording_counts: dict[str, int] | None,
) -> None:
    required_counts = _normalize_required_recording_counts(required_recording_counts)
    if not required_counts:
        return
    recorded_counts = Counter(str(recording.capture_target or "").strip() for recording in recordings)
    missing_counts = [
        f"{target} requires {required_count}, recorded {recorded_counts.get(target, 0)}"
        for target, required_count in required_counts.items()
        if recorded_counts.get(target, 0) < required_count
    ]
    if missing_counts:
        raise RuntimeError(
            "QA demo evidence is missing required recording count(s): " + "; ".join(missing_counts)
        )


def _validate_failure_evidence_cover_required_targets(
    *,
    failure_evidence: list[QaFailureEvidence],
    required_capture_targets: list[str] | tuple[str, ...] | None,
) -> None:
    required_targets = _normalize_required_capture_targets(required_capture_targets)
    if not required_targets:
        return
    evidence_targets = {
        str(item.capture_target or "").strip()
        for item in failure_evidence
        if str(item.capture_target or "").strip()
    }
    missing_targets = [target for target in required_targets if target not in evidence_targets]
    if missing_targets:
        raise RuntimeError(
            "QA demo failure evidence is missing required capture target(s): " + ", ".join(missing_targets)
        )


def _upload_failure_evidence(
    *,
    storage: DemoArtifactStorageConfig,
    tenant,  # noqa: ANN001
    project,  # noqa: ANN001
    run,  # noqa: ANN001
    proof_scope_id: str | None = None,
    local_evidence: list[LocalQaFailureEvidence],
    release_commit_sha: str,
    release_context_sha256: str,
) -> list[QaFailureEvidence]:
    uploaded: list[QaFailureEvidence] = []
    for index, item in enumerate(local_evidence, start=1):
        suffix = Path(item.path).suffix.lower()
        object_key = _demo_failure_artifact_object_key(
            tenant=tenant,
            project=project,
            run=run,
            proof_scope_id=proof_scope_id,
            index=index,
            suffix=suffix,
        )
        artifact_url = upload_recording(
            storage=storage,
            local_path=item.path,
            object_key=object_key,
            content_type=item.content_type,
            content_sha256=item.content_sha256,
            release_commit_sha=release_commit_sha,
            release_context_sha256=release_context_sha256,
        )
        uploaded.append(
            QaFailureEvidence(
                name=item.name,
                artifact_url=artifact_url,
                object_key=object_key,
                capture_target=item.capture_target,  # type: ignore[arg-type]
                capture_reference=item.capture_reference,
                error_message=item.error_message,
                content_sha256=item.content_sha256,
                release_commit_sha=release_commit_sha,
                release_context_sha256=release_context_sha256,
            )
        )
    return uploaded


def _validate_recordings_use_configured_storage(
    *,
    recordings: list[QaRecording],
    storage: DemoArtifactStorageConfig,
    tenant,
    project,
    run,
) -> None:
    public_base_url = storage.public_base_url.rstrip("/")
    expected_key_prefix = f"{tenant.tenant_id}/{project.project_id}/{run.run_id}/"
    for recording in recordings:
        artifact_url = str(recording.artifact_url or "").strip()
        if not artifact_url.startswith(f"{public_base_url}/"):
            raise RuntimeError(
                "QA demo recording must use configured artifact storage URL before PR evidence update: "
                f"{recording.capture_target}: {recording.name}"
            )
        object_key = str(recording.object_key or "").strip()
        if not object_key.startswith(expected_key_prefix):
            raise RuntimeError(
                "QA demo recording object key must be scoped to this run before PR evidence update: "
                f"{recording.capture_target}: {recording.name}"
            )
        expected_artifact_url = f"{public_base_url}/{object_key}"
        if artifact_url != expected_artifact_url:
            raise RuntimeError(
                "QA demo recording URL must match its uploaded object key before PR evidence update: "
                f"{recording.capture_target}: {recording.name}"
            )


def _validate_failure_evidence_use_configured_storage(
    *,
    failure_evidence: list[QaFailureEvidence],
    storage: DemoArtifactStorageConfig,
    tenant,
    project,
    run,
) -> None:
    public_base_url = storage.public_base_url.rstrip("/")
    expected_key_prefix = f"{tenant.tenant_id}/{project.project_id}/{run.run_id}/"
    for item in failure_evidence:
        artifact_url = str(item.artifact_url or "").strip()
        if not artifact_url.startswith(f"{public_base_url}/"):
            raise RuntimeError(
                "QA demo failure evidence must use configured artifact storage URL before PR evidence update: "
                f"{item.capture_target}: {item.name}"
            )
        object_key = str(item.object_key or "").strip()
        if not object_key.startswith(expected_key_prefix):
            raise RuntimeError(
                "QA demo failure evidence object key must be scoped to this run before PR evidence update: "
                f"{item.capture_target}: {item.name}"
            )
        expected_artifact_url = f"{public_base_url}/{object_key}"
        if artifact_url != expected_artifact_url:
            raise RuntimeError(
                "QA demo failure evidence URL must match its uploaded object key before PR evidence update: "
                f"{item.capture_target}: {item.name}"
            )


def _validate_qa_scenario_coverage(
    *,
    plan: PmPlan,
    qa_result: QaResult,
    capture_targets: dict[str, DemoCaptureTarget],
) -> None:
    target_names = set(capture_targets)
    required_counts = {
        target: count
        for target, count in required_recording_counts_by_target(plan).items()
        if target in target_names
    }
    scenarios_by_target: dict[str, list[QaScenario]] = {}
    missing_requirements: list[str] = []
    missing_variants: list[str] = []
    for scenario in qa_result.scenarios:
        if scenario.capture_target in target_names:
            scenarios_by_target.setdefault(scenario.capture_target, []).append(scenario)
    for requirement in plan.demo_requirements:
        if requirement.capture_target not in target_names:
            continue
        target_scenarios = scenarios_by_target.get(requirement.capture_target, [])
        normalized_title = _normalized_demo_text(requirement.title)
        normalized_acceptance_criterion = _normalized_demo_text(requirement.acceptance_criterion)
        if not any(
            normalized_title in _normalized_scenario_text(scenario)
            or normalized_acceptance_criterion in _normalized_scenario_text(scenario)
            for scenario in target_scenarios
        ):
            missing_requirements.append(
                f"{requirement.capture_target}: {requirement.title} ({requirement.acceptance_criterion})"
            )
        for variant in requirement.variants or []:
            normalized_variant = _normalized_demo_text(variant)
            if not any(normalized_variant in _normalized_scenario_text(scenario) for scenario in target_scenarios):
                missing_variants.append(f"{requirement.capture_target}: {variant}")
    missing = [
        f"{target}: expected at least {required_count}, got {len(scenarios_by_target.get(target, []))}"
        for target, required_count in required_counts.items()
        if len(scenarios_by_target.get(target, [])) < required_count
    ]
    messages: list[str] = []
    if missing:
        messages.append("insufficient scenario count: " + "; ".join(missing))
    if missing_requirements:
        messages.append("missing requirement coverage: " + "; ".join(missing_requirements))
    if missing_variants:
        messages.append("missing variant coverage: " + "; ".join(missing_variants))
    if messages:
        raise RuntimeError("QA demo scenarios do not cover PM demo requirement variants: " + " | ".join(messages))


def _validate_recorded_qa_result_covers_plan(*, plan: PmPlan, qa_result: QaResult) -> None:
    required_targets = set(required_capture_targets(plan))
    out_of_plan_targets = [
        target
        for target in recorded_capture_targets(qa_result.recordings)
        if target not in required_targets
    ]
    if out_of_plan_targets:
        raise RuntimeError(
            "QA demo previous recording proof contains capture target(s) outside the current PM demo plan: "
            + ", ".join(out_of_plan_targets)
        )
    capture_targets = {
        target: DemoCaptureTarget(capture_target=target, capture_reference="")
        for target in recorded_capture_targets(qa_result.recordings)
        if target in required_targets
    }
    if not capture_targets:
        return
    _validate_qa_scenario_coverage(
        plan=plan,
        qa_result=qa_result,
        capture_targets=capture_targets,
    )


def _validate_reusable_qa_recording_links(
    *,
    settings,  # noqa: ANN001
    storage: DemoArtifactStorageConfig,
    tenant,  # noqa: ANN001
    project,  # noqa: ANN001
    run,  # noqa: ANN001
    recordings: list[QaRecording],
    expected_release_commit_sha: str,
    expected_release_context_sha256: str,
) -> None:
    _validate_demo_evidence_recording_metadata(recordings)
    _validate_recordings_match_release_commit(
        recordings=recordings,
        expected_release_commit_sha=expected_release_commit_sha,
    )
    _validate_recordings_match_release_context(
        recordings=recordings,
        expected_release_context_sha256=expected_release_context_sha256,
    )
    _validate_distinct_demo_recording_artifacts(recordings)
    _validate_recordings_use_configured_storage(
        recordings=recordings,
        storage=storage,
        tenant=tenant,
        project=project,
        run=run,
    )
    for recording in recordings:
        ensure_artifact_url_reachable(
            recording.artifact_url,
            timeout_seconds=qa_demo_artifact_url_timeout_seconds(settings),
        )


def _normalized_demo_text(value: str) -> str:
    return " ".join(str(value or "").casefold().split())


def _normalized_scenario_text(scenario: QaScenario) -> str:
    return _normalized_demo_text(
        " ".join(
            [
                scenario.name,
                scenario.objective,
                *list(scenario.expected_outcomes or []),
            ]
        )
    )


def _copy_recordings(recordings: list[LocalQaRecording], *, request: WorkflowRequest) -> list[LocalQaRecording]:
    tenant_id = _require_safe_recording_scope_segment(
        request.tenant_id,
        field_name="tenant_id",
        scope_name="recording copy scope",
    )
    project_id = _require_safe_recording_scope_segment(
        request.project_id,
        field_name="project_id",
        scope_name="recording copy scope",
    )
    persisted_dir = Path.cwd() / "tmp" / "qa-demos" / tenant_id / project_id
    for segment in _artifact_scope_dir(request=request, scope_name="recording copy scope"):
        persisted_dir /= segment
    persisted_dir.mkdir(parents=True, exist_ok=True)
    copied: list[LocalQaRecording] = []
    for index, recording in enumerate(recordings, start=1):
        source = Path(recording.path)
        target = persisted_dir / f"{recording.capture_target}-{index}{source.suffix.lower()}"
        target.write_bytes(source.read_bytes())
        copied.append(
            LocalQaRecording(
                name=recording.name,
                path=str(target),
                capture_target=recording.capture_target,
                capture_reference=recording.capture_reference,
                content_type=recording.content_type,
                content_sha256=recording.content_sha256,
            )
        )
    return copied


def _copy_failure_evidence(
    evidence: list[LocalQaFailureEvidence],
    *,
    request: WorkflowRequest,
) -> list[LocalQaFailureEvidence]:
    tenant_id = _require_safe_recording_scope_segment(
        request.tenant_id,
        field_name="tenant_id",
        scope_name="failure evidence copy scope",
    )
    project_id = _require_safe_recording_scope_segment(
        request.project_id,
        field_name="project_id",
        scope_name="failure evidence copy scope",
    )
    persisted_dir = Path.cwd() / "tmp" / "qa-demos" / tenant_id / project_id
    for segment in _artifact_scope_dir(request=request, scope_name="failure evidence copy scope"):
        persisted_dir /= segment
    persisted_dir /= "failures"
    persisted_dir.mkdir(parents=True, exist_ok=True)
    copied: list[LocalQaFailureEvidence] = []
    for index, item in enumerate(evidence, start=1):
        source = Path(item.path)
        target = persisted_dir / f"{item.capture_target}-{index}{source.suffix.lower()}"
        target.write_bytes(source.read_bytes())
        copied.append(
            LocalQaFailureEvidence(
                name=item.name,
                path=str(target),
                capture_target=item.capture_target,
                capture_reference=item.capture_reference,
                content_type=item.content_type,
                content_sha256=item.content_sha256,
                error_message=item.error_message,
            )
        )
    return copied


def _capture_recording_terminal_failure_evidence(
    *,
    request: WorkflowRequest,
    capture_targets: dict[str, DemoCaptureTarget],
    error_message: str,
) -> list[LocalQaFailureEvidence]:
    tenant_id = _require_safe_recording_scope_segment(
        request.tenant_id,
        field_name="tenant_id",
        scope_name="failure evidence diagnostic scope",
    )
    project_id = _require_safe_recording_scope_segment(
        request.project_id,
        field_name="project_id",
        scope_name="failure evidence diagnostic scope",
    )
    artifact_scope = _artifact_scope_dir(request=request, scope_name="failure evidence diagnostic scope")
    persisted_dir = Path.cwd() / "tmp" / "qa-demos" / tenant_id / project_id
    for segment in artifact_scope:
        persisted_dir /= segment
    persisted_dir /= "failures"
    persisted_dir.mkdir(parents=True, exist_ok=True)

    evidence: list[LocalQaFailureEvidence] = []
    for capture_target_name in sorted(capture_targets):
        capture_target = capture_targets[capture_target_name]
        content = "\n".join(
            [
                "QA demo recording failed before video evidence was produced.",
                f"tenant_id: {tenant_id}",
                f"project_id: {project_id}",
                f"artifact_scope: {'/'.join(artifact_scope)}",
                f"capture_target: {capture_target.capture_target}",
                f"capture_reference: {capture_target.capture_reference}",
                "error:",
                error_message,
            ]
        ).strip() + "\n"
        target = persisted_dir / f"{capture_target.capture_target}-terminal-failure.txt"
        payload = content.encode("utf-8")
        target.write_bytes(payload)
        evidence.append(
            LocalQaFailureEvidence(
                name=f"{capture_target.capture_target} recorder failure",
                path=str(target),
                capture_target=capture_target.capture_target,
                capture_reference=capture_target.capture_reference,
                content_type="text/plain",
                content_sha256=hashlib.sha256(payload).hexdigest(),
                error_message=error_message,
            )
        )
    return evidence


def _capture_release_readiness_diagnostic_failure_evidence(
    *,
    request: WorkflowRequest,
    available_targets: dict[str, DemoCaptureTarget],
    required_capture_targets: tuple[str, ...],
    existing_evidence: list[LocalQaFailureEvidence],
    failure_message: str,
) -> list[LocalQaFailureEvidence]:
    existing_targets = {str(item.capture_target or "").strip() for item in existing_evidence}
    tenant_id = _require_safe_recording_scope_segment(
        request.tenant_id,
        field_name="tenant_id",
        scope_name="release readiness failure diagnostic scope",
    )
    project_id = _require_safe_recording_scope_segment(
        request.project_id,
        field_name="project_id",
        scope_name="release readiness failure diagnostic scope",
    )
    run_id = _require_safe_recording_scope_segment(
        request.run_id,
        field_name="run_id",
        scope_name="release readiness failure diagnostic scope",
    )
    persisted_dir = Path.cwd() / "tmp" / "qa-demos" / tenant_id / project_id / run_id / "failures"
    persisted_dir.mkdir(parents=True, exist_ok=True)

    evidence: list[LocalQaFailureEvidence] = []
    for raw_target in required_capture_targets:
        capture_target_name = str(raw_target or "").strip()
        if not capture_target_name or capture_target_name in existing_targets:
            continue
        safe_capture_target = _require_safe_recording_scope_segment(
            capture_target_name,
            field_name="capture_target",
            scope_name="release readiness failure diagnostic scope",
        )
        capture_target = available_targets.get(capture_target_name)
        capture_reference = (
            str(capture_target.capture_reference or "").strip()
            if capture_target is not None
            else f"{capture_target_name}://unavailable"
        )
        content = "\n".join(
            [
                "QA demo release readiness failed before target recording could start.",
                f"tenant_id: {tenant_id}",
                f"project_id: {project_id}",
                f"run_id: {run_id}",
                f"capture_target: {capture_target_name}",
                f"capture_reference: {capture_reference}",
                "error:",
                failure_message,
            ]
        ).strip() + "\n"
        target = persisted_dir / f"{safe_capture_target}-release-readiness-failure.txt"
        payload = content.encode("utf-8")
        target.write_bytes(payload)
        evidence.append(
            LocalQaFailureEvidence(
                name=f"{capture_target_name} release readiness failure",
                path=str(target),
                capture_target=capture_target_name,
                capture_reference=capture_reference,
                content_type="text/plain",
                content_sha256=hashlib.sha256(payload).hexdigest(),
                error_message=failure_message,
            )
        )
    return evidence


def _capture_release_readiness_failure_evidence(
    *,
    session,  # noqa: ANN001
    settings,  # noqa: ANN001
    tenant,  # noqa: ANN001
    project,  # noqa: ANN001
    request: WorkflowRequest,
    preview_release,  # noqa: ANN001
    release_service_urls: list[dict[str, str]],
    release_commit_sha: str,
    required_capture_targets: tuple[str, ...],
    failure_message: str,
) -> list[LocalQaFailureEvidence]:
    available_targets = resolve_available_capture_targets(settings=settings, preview_release=preview_release)
    diagnostic_failure_message = _append_failure_diagnostics(
        failure_message=failure_message,
        diagnostics=_release_readiness_failure_diagnostics(
            session=session,
            tenant=tenant,
            project=project,
            preview_release=preview_release,
            release_service_urls=release_service_urls,
        ),
    )
    browser_target = available_targets.get("browser")
    evidence: list[LocalQaFailureEvidence] = []
    if browser_target is not None and _capture_target_runs_on_worker(capture_target=browser_target, request=request):
        scenario = QaScenario(
            name="Release readiness failure",
            objective="Record the preview release failing to load before QA demo execution.",
            capture_target="browser",
            start_path="/",
            expected_outcomes=[diagnostic_failure_message],
            steps=[
                QaStep(
                    action="assert_visible",
                    selector="text=Master Builder QA release readiness passed",
                )
            ],
        )
        try:
            record_demo_scenarios(
                settings=settings,
                request=request,
                available_capture_targets={"browser": browser_target},
                qa_result=QaResult(
                    summary=[failure_message],
                    scenarios=[scenario],
                ),
                release_service_urls=release_service_urls,
                release_commit_sha=release_commit_sha,
            )
        except QaDemoRecordingFailure as exc:
            evidence.extend(
                _ensure_failure_evidence_has_diagnostics(
                    exc.failure_evidence,
                    failure_message=diagnostic_failure_message,
                )
            )
        except Exception as exc:  # noqa: BLE001
            diagnostic_failure_message = (
                f"{diagnostic_failure_message} Browser release-readiness failure recording did not produce video "
                "evidence: "
                f"{type(exc).__name__}: {exc}"
            )
    evidence.extend(
        _capture_release_readiness_diagnostic_failure_evidence(
            request=request,
            available_targets=available_targets,
            required_capture_targets=required_capture_targets,
            existing_evidence=evidence,
            failure_message=diagnostic_failure_message,
        )
    )
    return evidence


def _first_release_service_url(release_service_urls: list[dict[str, str]], *, service_kind: str) -> str:
    for service_url in release_service_urls:
        if service_url.get("service_kind") == service_kind:
            return str(service_url.get("url") or "").strip()
    return ""


def _first_release_service_recording_url(release_service_urls: list[dict[str, str]], *, service_kind: str) -> str:
    for service_url in release_service_urls:
        if service_url.get("service_kind") == service_kind:
            return str(service_url.get("recording_url") or service_url.get("url") or "").strip()
    return ""


def _first_release_service_recording_host_header(
    release_service_urls: list[dict[str, str]],
    *,
    service_kind: str,
) -> str:
    for service_url in release_service_urls:
        if service_url.get("service_kind") == service_kind:
            return str(service_url.get("recording_host_header") or "").strip()
    return ""


def _recorder_env_with_release_context(
    *,
    base_env: dict[str, str],
    release_service_urls: list[dict[str, str]],
    release_commit_sha: str = "",
) -> dict[str, str]:
    env = dict(base_env)
    serialized_urls = json.dumps(release_service_urls, sort_keys=True)
    release_commit_sha = str(release_commit_sha or "").strip()
    api_base_url = _first_release_service_url(release_service_urls, service_kind="api")
    browser_url = _first_release_service_url(release_service_urls, service_kind="website")
    api_recording_url = _first_release_service_recording_url(release_service_urls, service_kind="api")
    browser_recording_url = _first_release_service_recording_url(release_service_urls, service_kind="website")
    env["MB_QA_DEMO_RELEASE_COMMIT_SHA"] = release_commit_sha
    env["MB_QA_DEMO_RELEASE_SERVICE_URLS_JSON"] = serialized_urls
    env["MB_QA_DEMO_RELEASE_API_BASE_URL"] = api_base_url
    env["MB_QA_DEMO_RELEASE_BROWSER_URL"] = browser_url
    if api_recording_url:
        env["MB_QA_DEMO_RELEASE_API_RECORDING_URL"] = api_recording_url
    if browser_recording_url:
        env["MB_QA_DEMO_RELEASE_BROWSER_RECORDING_URL"] = browser_recording_url
    if api_recording_url or api_base_url:
        env["QA_DEMO_API_BASE_URL"] = api_recording_url or api_base_url
    if browser_recording_url or browser_url:
        env["QA_DEMO_BROWSER_URL"] = browser_recording_url or browser_url
    return env


def _require_safe_recording_scope_segment(value: str | None, *, field_name: str, scope_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError(f"QA demo {scope_name} requires {field_name}")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or not re.fullmatch(r"[A-Za-z0-9._-]+", raw):
        raise RuntimeError(f"QA demo {scope_name} has unsafe {field_name}: {raw!r}")
    return raw


def _require_safe_demo_proof_scope_segment(value: str | None, *, scope_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError(f"QA demo {scope_name} requires run_id or proof_scope_id")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or not re.fullmatch(r"[A-Za-z0-9._:-]+", raw):
        raise RuntimeError(f"QA demo {scope_name} has unsafe proof_scope_id: {raw!r}")
    return raw


def _demo_proof_scope_id_from_request(request: WorkflowRequest) -> str | None:
    context = request.trigger_context if isinstance(request.trigger_context, dict) else {}
    demo_context = context.get("demo_proof")
    if isinstance(demo_context, dict):
        raw_value = demo_context.get("proof_scope_id")
    else:
        raw_value = context.get("demo_proof_scope_id") or context.get("proof_scope_id")
    normalized = str(raw_value or "").strip()
    return normalized or None


def _artifact_scope_path(*, run, proof_scope_id: str | None, scope_name: str) -> str:  # noqa: ANN001
    raw_run_id = str(getattr(run, "run_id", None) or "").strip()
    if raw_run_id:
        return _require_safe_recording_scope_segment(
            raw_run_id,
            field_name="run_id",
            scope_name=scope_name,
        )
    proof_scope = _require_safe_demo_proof_scope_segment(proof_scope_id, scope_name=scope_name)
    return f"proofs/{proof_scope}"


def _artifact_scope_dir(*, request: WorkflowRequest, scope_name: str) -> tuple[str, ...]:
    raw_run_id = str(request.run_id or "").strip()
    if raw_run_id:
        return (
            _require_safe_recording_scope_segment(
                raw_run_id,
                field_name="run_id",
                scope_name=scope_name,
            ),
        )
    proof_scope = _require_safe_demo_proof_scope_segment(
        _demo_proof_scope_id_from_request(request),
        scope_name=scope_name,
    )
    return ("proofs", proof_scope)


def _demo_artifact_object_key(
    *,
    tenant,
    project,
    run,
    index: int,
    suffix: str,
    proof_scope_id: str | None = None,
) -> str:  # noqa: ANN001
    tenant_id = _require_safe_recording_scope_segment(
        getattr(tenant, "tenant_id", None),
        field_name="tenant_id",
        scope_name="artifact object key scope",
    )
    project_id = _require_safe_recording_scope_segment(
        getattr(project, "project_id", None),
        field_name="project_id",
        scope_name="artifact object key scope",
    )
    artifact_scope = _artifact_scope_path(
        run=run,
        proof_scope_id=proof_scope_id,
        scope_name="artifact object key scope",
    )
    return f"{tenant_id}/{project_id}/{artifact_scope}/qa-demo-{index}{suffix}"


def _demo_failure_artifact_object_key(
    *,
    tenant,
    project,
    run,
    index: int,
    suffix: str,
    proof_scope_id: str | None = None,
) -> str:  # noqa: ANN001
    tenant_id = _require_safe_recording_scope_segment(
        getattr(tenant, "tenant_id", None),
        field_name="tenant_id",
        scope_name="failure artifact object key scope",
    )
    project_id = _require_safe_recording_scope_segment(
        getattr(project, "project_id", None),
        field_name="project_id",
        scope_name="failure artifact object key scope",
    )
    artifact_scope = _artifact_scope_path(
        run=run,
        proof_scope_id=proof_scope_id,
        scope_name="failure artifact object key scope",
    )
    return f"{tenant_id}/{project_id}/{artifact_scope}/qa-failure-{index}{suffix}"


def _validate_recordings_cover_scenarios(
    *,
    capture_target_name: str,
    scenarios: list[QaScenario],
    recordings: list[LocalQaRecording],
) -> None:
    unmatched_scenario_names = [str(scenario.name or "").strip() for scenario in scenarios]
    planned_scenario_names = set(unmatched_scenario_names)
    seen_recording_names: set[str] = set()
    duplicate_recording_names: list[str] = []
    unplanned_recording_names: list[str] = []
    for recording in recordings:
        recording_name = str(recording.name or "").strip()
        if recording_name in seen_recording_names and recording_name not in duplicate_recording_names:
            duplicate_recording_names.append(recording_name or "<unnamed>")
            continue
        seen_recording_names.add(recording_name)
        if recording_name not in planned_scenario_names:
            unplanned_recording_names.append(recording_name or "<unnamed>")
            continue
        if recording_name in unmatched_scenario_names:
            unmatched_scenario_names.remove(recording_name)
    if duplicate_recording_names:
        raise RuntimeError(
            f"QA demo recorder produced duplicate recording(s) for {capture_target_name} scenario(s): "
            + ", ".join(duplicate_recording_names)
        )
    if unmatched_scenario_names:
        raise RuntimeError(
            f"QA demo recorder did not produce recording(s) for {capture_target_name} scenario(s): "
            + ", ".join(unmatched_scenario_names)
        )
    if unplanned_recording_names:
        raise RuntimeError(
            f"QA demo recorder produced unplanned recording(s) for {capture_target_name} scenario(s): "
            + ", ".join(unplanned_recording_names)
        )


def _validate_distinct_local_recording_content(
    *,
    capture_target_name: str,
    recordings: list[LocalQaRecording],
) -> None:
    seen_content: dict[str, str] = {}
    duplicate_recording_names: list[str] = []
    for recording in recordings:
        digest = str(recording.content_sha256 or "").strip().lower()
        recording_name = str(recording.name or "").strip() or "<unnamed>"
        previous_name = seen_content.get(digest)
        if previous_name is not None:
            duplicate_recording_names.extend([previous_name, recording_name])
            continue
        seen_content[digest] = recording_name
    if duplicate_recording_names:
        duplicates = list(dict.fromkeys(duplicate_recording_names))
        raise RuntimeError(
            f"QA demo recorder produced duplicate recording content for {capture_target_name} scenario(s): "
            + ", ".join(duplicates)
        )


def _validate_unique_scenario_names(*, capture_target_name: str, scenarios: list[QaScenario]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for scenario in scenarios:
        name = str(scenario.name or "").strip()
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise RuntimeError(
            f"QA demo scenarios for {capture_target_name} must have unique names: " + ", ".join(duplicates)
        )


def record_demo_scenarios(
    *,
    settings,  # noqa: ANN001
    request: WorkflowRequest,
    available_capture_targets: dict[str, DemoCaptureTarget],
    qa_result: QaResult,
    release_service_urls: list[dict[str, str]] | None = None,
    release_commit_sha: str = "",
) -> list[LocalQaRecording]:
    if not qa_result.scenarios:
        raise RuntimeError("QA demo recording requires at least one scenario")
    _validate_executable_qa_scenarios(qa_result)
    recorder_timeout_seconds = qa_demo_recorder_process_timeout_seconds(settings)

    scenarios_by_target: dict[str, list] = {}
    for scenario in qa_result.scenarios:
        target = available_capture_targets.get(scenario.capture_target)
        if target is None:
            raise RuntimeError(f"QA demo capture target is unavailable for this run: {scenario.capture_target}")
        if target.required_worker_platform is not None and request.current_worker_capability.value != target.required_worker_platform:
            raise RuntimeError(
                f"QA demo capture target '{scenario.capture_target}' requires worker platform {target.required_worker_platform}, "
                f"but run is on {request.current_worker_capability.value}"
            )
        scenarios_by_target.setdefault(scenario.capture_target, []).append(scenario)

    all_recordings: list[LocalQaRecording] = []
    release_service_urls = list(release_service_urls or [])
    release_commit_sha = str(release_commit_sha or "").strip()
    for capture_target_name, scenarios in scenarios_by_target.items():
        _validate_unique_scenario_names(capture_target_name=capture_target_name, scenarios=scenarios)
        capture_target = available_capture_targets[capture_target_name]
        payload = {
            "capture_target": capture_target.capture_target,
            "capture_reference": capture_target.capture_reference,
            "tenant_id": request.tenant_id,
            "project_id": request.project_id or "",
            "run_id": request.run_id,
            "issue_key": request.issue_key,
            "execution_repo_dir": request.execution_repo_dir or "",
            "target_source_paths": list(
                (request.project_demo_capture_target_sources or {}).get(capture_target_name, ())
            ),
            "release_commit_sha": release_commit_sha,
            "release_service_urls": release_service_urls,
            "release_api_base_url": _first_release_service_url(release_service_urls, service_kind="api"),
            "release_browser_url": _first_release_service_url(release_service_urls, service_kind="website"),
            "release_api_recording_url": _first_release_service_recording_url(
                release_service_urls,
                service_kind="api",
            ),
            "release_browser_recording_url": _first_release_service_recording_url(
                release_service_urls,
                service_kind="website",
            ),
            "scenarios": [
                {
                    "name": scenario.name,
                    "objective": scenario.objective,
                    "capture_target": scenario.capture_target,
                    "start_path": scenario.start_path,
                    "expected_outcomes": list(scenario.expected_outcomes or []),
                    "steps": [
                        {
                            "action": step.action,
                            "selector": step.selector,
                            "value": step.value,
                        }
                        for step in scenario.steps
                    ],
                }
                for scenario in scenarios
            ],
        }
        if capture_target.capture_target == "browser":
            _validate_browser_scenarios_stay_on_preview_origin(
                scenarios=scenarios,
                capture_reference=capture_target.capture_reference,
            )
            script_path = Path(__file__).resolve().parents[3] / "scripts" / "qa_demo_recorder.mjs"
            if not script_path.exists():
                raise RuntimeError(f"QA demo recorder script is missing: {script_path}")
            playwright_module_dir = str(getattr(settings, "qa_demo_playwright_module_dir", "") or "").strip()
            if not playwright_module_dir:
                try:
                    playwright_module_dir = subprocess.run(
                        ["npm", "root", "-g"],
                        capture_output=True,
                        check=True,
                        text=True,
                        timeout=recorder_timeout_seconds,
                    ).stdout.strip()
                except subprocess.TimeoutExpired as exc:
                    raise RuntimeError(
                        f"QA demo Playwright module lookup timed out after {recorder_timeout_seconds:.1f} seconds"
                    ) from exc
            env = dict(os.environ)
            env["NODE_PATH"] = playwright_module_dir
            env["QA_DEMO_PLAYWRIGHT_MODULE_DIR"] = playwright_module_dir
            env = _recorder_env_with_release_context(
                base_env=env,
                release_service_urls=release_service_urls,
                release_commit_sha=release_commit_sha,
            )
            payload["preview_url"] = capture_target.capture_reference
            browser_recording_url = _first_release_service_recording_url(
                release_service_urls,
                service_kind="website",
            )
            browser_recording_host_header = _first_release_service_recording_host_header(
                release_service_urls,
                service_kind="website",
            )
            if browser_recording_url:
                payload["recording_url"] = browser_recording_url
            if browser_recording_host_header:
                payload["recording_host_header"] = browser_recording_host_header
            recordings = _invoke_json_recorder(
                command=["node", str(script_path)],
                payload=payload,
                env=env,
                capture_target=capture_target,
                request=request,
                timeout_seconds=recorder_timeout_seconds,
            )
        else:
            recordings = _invoke_json_recorder(
                command=list(capture_target.recorder_command or ()),
                payload=payload,
                env=_recorder_env_with_release_context(
                    base_env=dict(os.environ),
                    release_service_urls=release_service_urls,
                    release_commit_sha=release_commit_sha,
                ),
                capture_target=capture_target,
                request=request,
                timeout_seconds=recorder_timeout_seconds,
            )
        _validate_recordings_cover_scenarios(
            capture_target_name=capture_target_name,
            scenarios=scenarios,
            recordings=recordings,
        )
        _validate_distinct_local_recording_content(
            capture_target_name=capture_target_name,
            recordings=recordings,
        )
        all_recordings.extend(recordings)
    return all_recordings


def upload_recording(
    *,
    storage: DemoArtifactStorageConfig,
    local_path: str,
    object_key: str,
    content_type: str,
    content_sha256: str,
    release_commit_sha: str,
    release_context_sha256: str,
) -> str:
    from minio import Minio

    digest = _require_content_sha256_value(content_sha256)
    release_commit = _require_release_commit_sha_value(release_commit_sha)
    release_context_digest = _require_content_sha256_value(release_context_sha256)
    client = Minio(
        storage.endpoint,
        access_key=storage.access_key,
        secret_key=storage.secret_key,
        secure=storage.secure,
    )
    if not client.bucket_exists(storage.bucket):
        raise RuntimeError(f"QA demo artifact bucket does not exist: {storage.bucket}")
    client.fput_object(
        storage.bucket,
        object_key,
        local_path,
        content_type=content_type,
        metadata={
            _QA_DEMO_CONTENT_SHA256_METADATA_KEY: digest,
            _QA_DEMO_RELEASE_COMMIT_SHA_METADATA_KEY: release_commit,
            _QA_DEMO_RELEASE_CONTEXT_SHA256_METADATA_KEY: release_context_digest,
        },
    )
    object_stat = client.stat_object(storage.bucket, object_key)
    stored_digest = _metadata_sha256(
        getattr(object_stat, "metadata", None),
        keys=(_QA_DEMO_CONTENT_SHA256_METADATA_KEY, _QA_DEMO_CONTENT_SHA256_METADATA_HEADER),
    )
    if stored_digest != digest:
        raise RuntimeError(
            "QA demo artifact metadata sha256 mismatch after upload: "
            f"{object_key} expected {digest}, got {stored_digest or '<missing>'}"
        )
    stored_release_commit = _metadata_sha256(
        getattr(object_stat, "metadata", None),
        keys=(_QA_DEMO_RELEASE_COMMIT_SHA_METADATA_KEY, _QA_DEMO_RELEASE_COMMIT_SHA_METADATA_HEADER),
    )
    if stored_release_commit != release_commit:
        raise RuntimeError(
            "QA demo artifact metadata release commit sha mismatch after upload: "
            f"{object_key} expected {release_commit}, got {stored_release_commit or '<missing>'}"
        )
    stored_release_context_digest = _metadata_sha256(
        getattr(object_stat, "metadata", None),
        keys=(_QA_DEMO_RELEASE_CONTEXT_SHA256_METADATA_KEY, _QA_DEMO_RELEASE_CONTEXT_SHA256_METADATA_HEADER),
    )
    if stored_release_context_digest != release_context_digest:
        raise RuntimeError(
            "QA demo artifact metadata release context sha256 mismatch after upload: "
            f"{object_key} expected {release_context_digest}, got {stored_release_context_digest or '<missing>'}"
        )
    return f"{storage.public_base_url}/{object_key}"


def _require_content_sha256_value(value: object) -> str:
    digest = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeError("QA demo recording content sha256 is required before artifact upload")
    return digest


def _require_release_commit_sha_value(value: object) -> str:
    commit_sha = str(value or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", commit_sha):
        raise RuntimeError("QA demo recording release commit sha is required before artifact upload")
    return commit_sha


def _metadata_sha256(metadata: object, *, keys: tuple[str, str]) -> str:
    items = getattr(metadata, "items", None)
    if not callable(items):
        return ""
    normalized_keys = {key.lower() for key in keys}
    for raw_key, raw_value in items():
        normalized_key = str(raw_key or "").strip().lower()
        if normalized_key in normalized_keys:
            return str(raw_value or "").strip().lower()
    return ""


def execute_qa_demo_stage(
    *,
    session,
    settings,
    tenant,
    project,
    run,
    request: WorkflowRequest,
    plan,
    dev_result: DevResult,
    test_result: TestResult,
    review_result: ReviewResult,
    preview_release,
    previous_qa_result: QaResult | None = None,
) -> QaResult:
    ensure_pm_demo_requirements_are_recordable(plan)
    ensure_pm_demo_requirements_cover_project_targets(plan=plan, request=request)
    required_targets = required_capture_targets(plan)
    if previous_qa_result is not None:
        previous_qa_result = _validate_recorded_scenario_proof(previous_qa_result)
        _validate_recorded_qa_result_covers_plan(plan=plan, qa_result=previous_qa_result)
    previous_recordings = list(previous_qa_result.recordings if previous_qa_result is not None else [])
    previous_scenarios = list(previous_qa_result.scenarios if previous_qa_result is not None else [])
    proof_scope_id = _demo_proof_scope_id_from_request(request)
    release_service_urls = _release_service_urls_payload(preview_release)
    recorder_release_service_urls = _release_service_urls_payload(
        preview_release,
        include_recording_details=True,
    )
    try:
        ensure_release_ready_for_qa(
            preview_release,
            required_service_kinds=required_release_service_kinds(project=project, preview_release=preview_release),
            service_url_probe=_default_service_url_probe,
            timeout_seconds=qa_demo_release_health_timeout_seconds(settings),
        )
    except RuntimeError as exc:
        message = f"QA demo recording cannot start because the release did not load: {exc}"
        release_commit_sha = str(getattr(preview_release, "commit_sha", "") or "").strip()
        local_failure_evidence = _capture_release_readiness_failure_evidence(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            request=request,
            preview_release=preview_release,
            release_service_urls=recorder_release_service_urls,
            release_commit_sha=release_commit_sha,
            required_capture_targets=required_targets,
            failure_message=message,
        )
        if not local_failure_evidence:
            raise
        try:
            release_context_sha256 = release_context_sha256_for_release(
                release=preview_release,
                release_service_urls=release_service_urls,
            )
        except RuntimeError:
            raise exc
        try:
            uploaded_failure_evidence = _upload_failure_evidence(
                storage=storage_config_from_settings(settings),
                tenant=tenant,
                project=project,
                run=run,
                proof_scope_id=proof_scope_id,
                local_evidence=local_failure_evidence,
                release_commit_sha=release_commit_sha,
                release_context_sha256=release_context_sha256,
            )
            for item in uploaded_failure_evidence:
                ensure_artifact_url_reachable(
                    item.artifact_url,
                    timeout_seconds=qa_demo_artifact_url_timeout_seconds(settings),
                )
        except RuntimeError as upload_exc:
            raise RuntimeError(
                f"{message} QA demo release failure evidence could not be uploaded: {upload_exc}"
            ) from upload_exc
        if uploaded_failure_evidence:
            return QaResult(
                summary=[message],
                scenarios=[],
                recordings=previous_recordings,
                failure_evidence=uploaded_failure_evidence,
                outcome="blocked",
                blocker_message=message,
            )
    release_context_sha256 = release_context_sha256_for_release(
        release=preview_release,
        release_service_urls=release_service_urls,
    )
    release_commit_sha = str(getattr(preview_release, "commit_sha", "") or "").strip()
    if previous_recordings:
        _validate_reusable_qa_recording_links(
            settings=settings,
            storage=storage_config_from_settings(settings),
            tenant=tenant,
            project=project,
            run=run,
            recordings=previous_recordings,
            expected_release_commit_sha=release_commit_sha,
            expected_release_context_sha256=release_context_sha256,
        )
    remaining_targets = remaining_capture_targets(plan, previous_recordings)
    if not remaining_targets:
        _validate_recordings_cover_required_targets(
            recordings=previous_recordings,
            required_capture_targets=required_targets,
        )
        _validate_recordings_cover_required_counts(
            recordings=previous_recordings,
            required_recording_counts=required_recording_counts_by_target(plan),
        )
        return QaResult(
            summary=[f"QA demo recording already has proof for required target(s): {', '.join(required_targets)}."],
            scenarios=previous_scenarios,
            recordings=previous_recordings,
            outcome="continue",
        )
    available_capture_targets = resolve_available_capture_targets(settings=settings, preview_release=preview_release)
    if not available_capture_targets:
        raise RuntimeError("QA demo recording requires at least one configured capture target")
    missing_targets = [target for target in remaining_targets if target not in available_capture_targets]
    if missing_targets:
        raise RuntimeError(
            "QA demo recording requires unavailable capture target(s): " + ", ".join(sorted(missing_targets))
        )
    remaining_available_capture_targets = {
        target_name: available_capture_targets[target_name]
        for target_name in remaining_targets
    }
    current_worker_capture_targets = {
        target_name: target
        for target_name, target in remaining_available_capture_targets.items()
        if _capture_target_runs_on_worker(capture_target=target, request=request)
    }
    if not current_worker_capture_targets:
        message = (
            "QA demo recording requires another worker platform for remaining capture target(s): "
            + ", ".join(remaining_targets)
        )
        return QaResult(
            summary=[message],
            scenarios=previous_scenarios,
            recordings=previous_recordings,
            outcome="requeue",
            feedback=message,
        )
    for capture_target in current_worker_capture_targets.values():
        ensure_capture_target_runtime_ready(capture_target)
    browser_capture_reference = (
        current_worker_capture_targets.get("browser").capture_reference
        if "browser" in current_worker_capture_targets
        else ""
    )
    runtime = build_codex_runtime(session=session, settings=settings)
    agents = CodexWorkflowAgents(
        runtime=runtime,
        runtime_resolver=lambda stage, qa_request: build_runtime_for_selector(
            session=session,
            settings=settings,
            tenant_id=qa_request.tenant_id,
            project_id=qa_request.project_id,
            selector=f"workflow.{stage}",
            agent_role="test" if stage == "qa" else None,
            agent_name="workflow_qa_default" if stage == "qa" else None,
        ),
        execute_tool=lambda context, tool_name, tool_args: execute_agent_tool(
            session=session,
            settings=settings,
            tenant_id=context.tenant_id or "",
            project_id=context.project_id,
            run_id=context.run_id,
            issue_key=context.issue_key or "",
            stage=context.stage,
            tool_name=tool_name,
            tool_args=tool_args,
            worker_platform=context.worker_platform,
        ),
    )
    qa_result = agents.qa(
        request=request,
        plan=plan,
        dev_result=dev_result,
        test_result=test_result,
        review_result=review_result,
        browser_capture_reference=browser_capture_reference,
        available_capture_targets_json=json.dumps(
            _available_capture_targets_payload(
                current_worker_capture_targets,
                source_paths_by_target=request.project_demo_capture_target_sources,
                release_service_urls=release_service_urls,
                release_commit_sha=release_commit_sha,
            )
        ),
        attempt=request.attempt_number,
    )
    if qa_result.outcome != "continue":
        return qa_result
    qa_result = _validate_native_scenarios(qa_result)
    _validate_executable_qa_scenarios(qa_result)
    _validate_qa_scenario_coverage(
        plan=plan,
        qa_result=qa_result,
        capture_targets=current_worker_capture_targets,
    )
    storage = storage_config_from_settings(settings)

    max_attempts = qa_demo_max_attempts(settings)
    last_error: Exception | None = None
    last_failure_evidence: list[LocalQaFailureEvidence] = []
    for attempt in range(1, max_attempts + 1):
        try:
            local_recordings = record_demo_scenarios(
                settings=settings,
                request=request,
                available_capture_targets=current_worker_capture_targets,
                qa_result=qa_result,
                release_service_urls=recorder_release_service_urls,
                release_commit_sha=release_commit_sha,
            )
            uploaded: list[QaRecording] = []
            for index, recording in enumerate(local_recordings, start=1):
                suffix = Path(recording.path).suffix.lower()
                object_key = _demo_artifact_object_key(
                    tenant=tenant,
                    project=project,
                    run=run,
                    proof_scope_id=proof_scope_id,
                    index=len(previous_recordings) + index,
                    suffix=suffix,
                )
                artifact_url = upload_recording(
                    storage=storage,
                    local_path=recording.path,
                    object_key=object_key,
                    content_type=recording.content_type,
                    content_sha256=recording.content_sha256,
                    release_commit_sha=release_commit_sha,
                    release_context_sha256=release_context_sha256,
                )
                ensure_artifact_url_reachable(
                    artifact_url,
                    timeout_seconds=qa_demo_artifact_url_timeout_seconds(settings),
                )
                uploaded.append(
                    QaRecording(
                        name=recording.name,
                        artifact_url=artifact_url,
                        object_key=object_key,
                        capture_target=recording.capture_target,
                        capture_reference=recording.capture_reference,
                        content_sha256=recording.content_sha256,
                        release_commit_sha=release_commit_sha,
                        release_context_sha256=release_context_sha256,
                    )
                )
            combined_recordings = [*previous_recordings, *uploaded]
            combined_scenarios = [*previous_scenarios, *qa_result.scenarios]
            combined_result = _validate_recorded_scenario_proof(
                replace(qa_result, scenarios=combined_scenarios, recordings=combined_recordings)
            )
            _validate_recorded_qa_result_covers_plan(plan=plan, qa_result=combined_result)
            still_remaining = remaining_capture_targets(plan, combined_recordings)
            if still_remaining:
                missing_current_worker_targets = [
                    target for target in still_remaining if target in current_worker_capture_targets
                ]
                if missing_current_worker_targets:
                    raise RuntimeError(
                        "QA demo recording did not produce proof for current worker capture target(s): "
                        + ", ".join(missing_current_worker_targets)
                    )
                message = (
                    "QA demo recording produced proof for current worker target(s) but still requires capture target(s): "
                    + ", ".join(still_remaining)
                )
                return replace(
                    combined_result,
                    summary=[*list(combined_result.summary or []), message],
                    outcome="requeue",
                    feedback=message,
                    blocker_message=None,
                )
            return combined_result
        except QaDemoRecordingFailure as exc:
            last_error = exc
            last_failure_evidence = list(exc.failure_evidence)
            if attempt >= max_attempts:
                break
        except Exception as exc:  # noqa: BLE001
            if "QA demo artifact object key scope has unsafe" in str(exc):
                raise
            last_error = exc
            if attempt >= max_attempts:
                break
    if last_error is None:  # pragma: no cover
        raise RuntimeError("QA demo recording failed without an exception")
    message = f"QA demo recording failed after {max_attempts} attempts: {type(last_error).__name__}: {last_error}"
    if not last_failure_evidence:
        last_failure_evidence = _capture_recording_terminal_failure_evidence(
            request=request,
            capture_targets=current_worker_capture_targets,
            error_message=message,
        )
    uploaded_failure_evidence = _upload_failure_evidence(
        storage=storage,
        tenant=tenant,
        project=project,
        run=run,
        proof_scope_id=proof_scope_id,
        local_evidence=last_failure_evidence,
        release_commit_sha=release_commit_sha,
        release_context_sha256=release_context_sha256,
    )
    for item in uploaded_failure_evidence:
        ensure_artifact_url_reachable(
            item.artifact_url,
            timeout_seconds=qa_demo_artifact_url_timeout_seconds(settings),
        )
    return QaResult(
        summary=[message],
        scenarios=list(qa_result.scenarios),
        recordings=previous_recordings,
        failure_evidence=uploaded_failure_evidence,
        outcome="blocked",
        blocker_message=message,
    )


def _load_pull_request_update_context(
    *,
    session,
    settings,
    tenant,
    project,
    workflow_result,
) -> _PullRequestBodyUpdateContext:
    pr_url = str(getattr(workflow_result, "pr_url", "") or "").strip()
    match = re.search(r"/pull/(\d+)(?:/|$)", pr_url)
    if not pr_url or match is None:
        raise RuntimeError("QA demo recording requires a PR URL")
    normalized_repo = normalize_repo_identifier(project.github_repository)
    pr_repo = _repo_full_name_from_pull_request_url(pr_url)
    repo_full_name = _repo_full_name_from_normalized_repo(normalized_repo)
    if pr_repo != repo_full_name:
        raise RuntimeError(
            "QA demo recording PR URL must match project repository before PR evidence update: "
            f"{pr_repo or '<unknown>'} != {repo_full_name}"
        )
    pr_number = int(match.group(1))
    github_client = github_client_from_tenant_config(
        tenant.github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )
    pr_details = github_client.get_pull_request_details(repo_full_name=repo_full_name, pr_number=pr_number)
    base_branch = str(pr_details.base_ref or "").strip()
    if not base_branch:
        raise RuntimeError("QA demo evidence PR update requires PR details to include a base branch")
    return _PullRequestBodyUpdateContext(
        github_client=github_client,
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        title=pr_details.title,
        base_branch=base_branch,
        body=str(pr_details.body or ""),
    )


def _persist_pull_request_body(
    *,
    context: _PullRequestBodyUpdateContext,
    project,
    body: str,
) -> None:
    context.github_client.update_pull_request(
        repo_full_name=context.repo_full_name,
        github_repository=project.github_repository,
        pr_number=context.pr_number,
        title=context.title,
        base_branch=context.base_branch,
        body=body,
    )
    updated_pr_details = context.github_client.get_pull_request_details(
        repo_full_name=context.repo_full_name,
        pr_number=context.pr_number,
    )
    if updated_pr_details.body != body:
        raise RuntimeError("QA demo evidence PR update did not persist required evidence")


def update_pull_request_with_demo_evidence(
    *,
    session,
    settings,
    tenant,
    project,
    run,
    workflow_result,
    qa_result: QaResult,
    required_capture_targets: list[str] | tuple[str, ...] | None = None,
    required_recording_counts: dict[str, int] | None = None,
) -> str:
    if not qa_result.recordings:
        raise RuntimeError("QA demo evidence PR update requires at least one recording")
    qa_result = _validate_recorded_scenario_proof(qa_result)
    _validate_demo_evidence_recording_metadata(qa_result.recordings)
    _validate_distinct_demo_recording_artifacts(qa_result.recordings)
    _validate_recordings_cover_required_targets(
        recordings=qa_result.recordings,
        required_capture_targets=required_capture_targets,
    )
    _validate_recordings_cover_required_counts(
        recordings=qa_result.recordings,
        required_recording_counts=required_recording_counts,
    )
    storage = storage_config_from_settings(settings)
    _validate_recordings_use_configured_storage(
        recordings=qa_result.recordings,
        storage=storage,
        tenant=tenant,
        project=project,
        run=run,
    )
    for recording in qa_result.recordings:
        ensure_artifact_url_reachable(
            recording.artifact_url,
            timeout_seconds=qa_demo_artifact_url_timeout_seconds(settings),
        )
    pr_context = _load_pull_request_update_context(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        workflow_result=workflow_result,
    )
    body = upsert_demo_evidence_section(
        body=pr_context.body,
        recordings=qa_result.recordings,
        required_capture_targets=required_capture_targets,
        required_recording_counts=required_recording_counts,
    )
    _persist_pull_request_body(context=pr_context, project=project, body=body)
    return body


def mark_pull_request_ready_after_demo_proof(
    *,
    session,
    settings,
    tenant,
    project,
    workflow_result,
) -> None:
    pr_context = _load_pull_request_update_context(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        workflow_result=workflow_result,
    )
    pr_context.github_client.mark_pull_request_ready_for_review(
        repo_full_name=pr_context.repo_full_name,
        pr_number=pr_context.pr_number,
    )


def update_pull_request_with_demo_failure_evidence(
    *,
    session,
    settings,
    tenant,
    project,
    run,
    workflow_result,
    qa_result: QaResult,
    required_capture_targets: list[str] | tuple[str, ...] | None = None,
) -> str:
    if not qa_result.failure_evidence:
        raise RuntimeError("QA demo failure evidence PR update requires at least one failure artifact")
    _validate_demo_failure_evidence_metadata(qa_result.failure_evidence)
    _validate_distinct_demo_failure_evidence_artifacts(qa_result.failure_evidence)
    _validate_failure_evidence_cover_required_targets(
        failure_evidence=qa_result.failure_evidence,
        required_capture_targets=required_capture_targets,
    )
    storage = storage_config_from_settings(settings)
    _validate_failure_evidence_use_configured_storage(
        failure_evidence=qa_result.failure_evidence,
        storage=storage,
        tenant=tenant,
        project=project,
        run=run,
    )
    for item in qa_result.failure_evidence:
        ensure_artifact_url_reachable(
            item.artifact_url,
            timeout_seconds=qa_demo_artifact_url_timeout_seconds(settings),
        )
    pr_context = _load_pull_request_update_context(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        workflow_result=workflow_result,
    )
    body = upsert_demo_failure_evidence_section(
        body=pr_context.body,
        failure_evidence=qa_result.failure_evidence,
    )
    _persist_pull_request_body(context=pr_context, project=project, body=body)
    return body


def _repo_full_name_from_normalized_repo(normalized_repo: str) -> str:
    normalized = str(normalized_repo or "").strip().strip("/")
    if normalized.startswith("github.com/"):
        return normalized.split("/", 1)[1]
    return "/".join(normalized.split("/")[-2:])


def _repo_full_name_from_pull_request_url(pr_url: str) -> str | None:
    parsed = urllib.parse.urlparse(str(pr_url or "").strip())
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 4 or parts[2] != "pull":
        return None
    return f"{parts[0].lower()}/{parts[1].lower()}"
