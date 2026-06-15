from __future__ import annotations

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

from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.runtime.agents import CodexWorkflowAgents
from orchestrator.core.runtime.runtime import build_codex_runtime
from orchestrator.core.runtime.tools import execute_agent_tool
from orchestrator.core.qa.mobile_xcuitest_recorder import preferred_simulator_udid
from orchestrator.core.worker.capability_normalization import parse_worker_capability
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
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
_TRANSIENT_NATIVE_SELECTORS = frozenset({"id=splash_screen", "splash_screen"})
_DEMO_CAPTURE_TARGETS = frozenset({"browser", "ios", "android", "desktop"})
_NATIVE_CAPTURE_TARGETS = frozenset({"ios", "android", "desktop"})
_RELEASE_SERVICE_READY_STATUSES = frozenset({401, 403, 405})
_QA_PROOF_STEP_ACTIONS = frozenset({"assert_visible", "assert_text", "wait_for_text", "wait_for_url"})


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


def _default_service_url_probe(url: str, *, timeout_seconds: float) -> int:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "MasterBuilder-QA-Demo/1.0"},
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
        active_urls = [
            str(getattr(service_url, "url", "") or "").strip()
            for service_url in matching_urls
            if str(getattr(service_url, "status", "") or "").strip() == "active"
            and str(getattr(service_url, "url", "") or "").strip()
        ]
        if not active_urls:
            inactive_failures.append(required_kind)
            continue
        if service_url_probe is None:
            continue
        for active_url in active_urls:
            try:
                status_code = int(service_url_probe(active_url, timeout_seconds=timeout_seconds))
            except Exception as exc:  # noqa: BLE001
                probe_failures.append(f"{required_kind} ({active_url}): {exc}")
                continue
            if not _release_service_status_is_ready(status_code):
                probe_failures.append(f"{required_kind} ({active_url}): HTTP {status_code}")
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


def _is_builtin_android_recorder_command(command: tuple[str, ...] | None) -> bool:
    return any(str(part).endswith("qa_demo_android_recorder.py") for part in (command or ()))


def _is_builtin_ios_recorder_command(command: tuple[str, ...] | None) -> bool:
    return any(str(part).endswith("qa_demo_mobile_recorder.py") for part in (command or ()))


def _required_android_recorder_tool(*, env_var: str | None, default: str) -> str:
    if env_var is None:
        return default
    return str(os.environ.get(env_var) or default).strip()


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
    if not _ready_adb_devices(result.stdout):
        raise RuntimeError("No available Android emulator/device found via adb devices")


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
            required_worker_platform="linux",
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
    constraints["android"] = PlannedCaptureTargetConstraint(
        capture_target="android",
        provider_available=android_recorder_command is not None,
        required_worker_platform="linux" if android_recorder_command is not None else None,
        availability_reason=(
            "Android capture requires the built-in or configured native recorder on a Linux Android worker. Runtime readiness is verified on the selected worker before recording."
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
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for target in targets.values():
        payload.append(
            {
                "capture_target": target.capture_target,
                "capture_reference": target.capture_reference,
                "source_paths": list((source_paths_by_target or {}).get(target.capture_target, ())),
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
    missing_variants = [
        f"{requirement.capture_target}: {requirement.title}"
        for requirement in plan.demo_requirements
        if not list(requirement.variants or [])
    ]
    if missing_variants:
        raise RuntimeError(
            "QA demo recording requires PM demo requirement variants for QA walkthrough coverage: "
            + "; ".join(missing_variants)
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
        _require_demo_evidence_field(recording.artifact_url, field="artifact_url")


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
        artifact_url = _require_demo_evidence_field(recording.artifact_url, field="artifact_url")
        lines.append(
            f"- {name} "
            f"[target={capture_target}; reference={capture_reference}; object_key={object_key}]: "
            f"{artifact_url}"
        )
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


def _content_type_for_recording(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mp4":
        return "video/mp4"
    if suffix == ".mov":
        return "video/quicktime"
    raise RuntimeError(f"Unsupported QA demo recording file type: {suffix or '<none>'}")


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
        recordings.append(
            LocalQaRecording(
                name=name,
                path=str(source),
                capture_target=capture_target.capture_target,
                capture_reference=capture_target.capture_reference,
                content_type=_content_type_for_recording(source),
            )
        )
    if not recordings:
        raise RuntimeError("QA demo recorder produced no recordings")
    return recordings


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
            subprocess.run(
                [*command, str(input_path), str(output_path)],
                check=True,
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


def _validate_reusable_qa_recordings(
    *,
    settings,  # noqa: ANN001
    storage: DemoArtifactStorageConfig,
    tenant,  # noqa: ANN001
    project,  # noqa: ANN001
    run,  # noqa: ANN001
    plan: PmPlan,
    recordings: list[QaRecording],
) -> None:
    _validate_demo_evidence_recording_metadata(recordings)
    _validate_distinct_demo_recording_artifacts(recordings)
    _validate_recordings_use_configured_storage(
        recordings=recordings,
        storage=storage,
        tenant=tenant,
        project=project,
        run=run,
    )
    _validate_recordings_cover_required_targets(
        recordings=recordings,
        required_capture_targets=required_capture_targets(plan),
    )
    _validate_recordings_cover_required_counts(
        recordings=recordings,
        required_recording_counts=required_recording_counts_by_target(plan),
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
    run_id = _require_safe_recording_scope_segment(
        request.run_id,
        field_name="run_id",
        scope_name="recording copy scope",
    )
    persisted_dir = Path.cwd() / "tmp" / "qa-demos" / tenant_id / project_id / run_id
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
            )
        )
    return copied


def _require_safe_recording_scope_segment(value: str | None, *, field_name: str, scope_name: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise RuntimeError(f"QA demo {scope_name} requires {field_name}")
    if raw in {".", ".."} or "/" in raw or "\\" in raw or not re.fullmatch(r"[A-Za-z0-9._-]+", raw):
        raise RuntimeError(f"QA demo {scope_name} has unsafe {field_name}: {raw!r}")
    return raw


def _demo_artifact_object_key(*, tenant, project, run, index: int, suffix: str) -> str:  # noqa: ANN001
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
    run_id = _require_safe_recording_scope_segment(
        getattr(run, "run_id", None),
        field_name="run_id",
        scope_name="artifact object key scope",
    )
    return f"{tenant_id}/{project_id}/{run_id}/qa-demo-{index}{suffix}"


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
            payload["preview_url"] = capture_target.capture_reference
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
                env=dict(os.environ),
                capture_target=capture_target,
                request=request,
                timeout_seconds=recorder_timeout_seconds,
            )
        _validate_recordings_cover_scenarios(
            capture_target_name=capture_target_name,
            scenarios=scenarios,
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
) -> str:
    from minio import Minio

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
    )
    return f"{storage.public_base_url}/{object_key}"


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
    required_targets = required_capture_targets(plan)
    if previous_qa_result is not None:
        previous_qa_result = _validate_recorded_scenario_proof(previous_qa_result)
    previous_recordings = list(previous_qa_result.recordings if previous_qa_result is not None else [])
    previous_scenarios = list(previous_qa_result.scenarios if previous_qa_result is not None else [])
    remaining_targets = remaining_capture_targets(plan, previous_recordings)
    if not remaining_targets:
        _validate_reusable_qa_recordings(
            settings=settings,
            storage=storage_config_from_settings(settings),
            tenant=tenant,
            project=project,
            run=run,
            plan=plan,
            recordings=previous_recordings,
        )
        return QaResult(
            summary=[f"QA demo recording already has proof for required target(s): {', '.join(required_targets)}."],
            scenarios=previous_scenarios,
            recordings=previous_recordings,
            outcome="continue",
        )
    ensure_release_ready_for_qa(
        preview_release,
        required_service_kinds=required_release_service_kinds(project=project, preview_release=preview_release),
        service_url_probe=_default_service_url_probe,
        timeout_seconds=qa_demo_release_health_timeout_seconds(settings),
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
    for attempt in range(1, max_attempts + 1):
        try:
            local_recordings = record_demo_scenarios(
                settings=settings,
                request=request,
                available_capture_targets=current_worker_capture_targets,
                qa_result=qa_result,
            )
            uploaded: list[QaRecording] = []
            for index, recording in enumerate(local_recordings, start=1):
                suffix = Path(recording.path).suffix.lower()
                object_key = _demo_artifact_object_key(
                    tenant=tenant,
                    project=project,
                    run=run,
                    index=len(previous_recordings) + index,
                    suffix=suffix,
                )
                artifact_url = upload_recording(
                    storage=storage,
                    local_path=recording.path,
                    object_key=object_key,
                    content_type=recording.content_type,
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
                    )
                )
            combined_recordings = [*previous_recordings, *uploaded]
            combined_scenarios = [*previous_scenarios, *qa_result.scenarios]
            combined_result = _validate_recorded_scenario_proof(
                replace(qa_result, scenarios=combined_scenarios, recordings=combined_recordings)
            )
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
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= max_attempts:
                break
    if last_error is None:  # pragma: no cover
        raise RuntimeError("QA demo recording failed without an exception")
    raise RuntimeError(
        f"QA demo recording failed after {max_attempts} attempts: {type(last_error).__name__}: {last_error}"
    ) from last_error


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
    pr_url = str(getattr(workflow_result, "pr_url", "") or "").strip()
    match = re.search(r"/pull/(\d+)(?:/|$)", pr_url)
    if not pr_url or match is None:
        raise RuntimeError("QA demo recording requires a PR URL")
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
    normalized_repo = normalize_repo_identifier(project.github_repository)
    repo_full_name = normalized_repo.split("/", 1)[1] if normalized_repo.startswith("github.com/") else "/".join(normalized_repo.split("/")[-2:])
    pr_details = github_client.get_pull_request_details(repo_full_name=repo_full_name, pr_number=pr_number)
    body = upsert_demo_evidence_section(
        body=pr_details.body,
        recordings=qa_result.recordings,
        required_capture_targets=required_capture_targets,
        required_recording_counts=required_recording_counts,
    )
    github_client.update_pull_request(
        repo_full_name=repo_full_name,
        github_repository=project.github_repository,
        pr_number=pr_number,
        title=pr_details.title,
        base_branch=pr_details.base_ref or "main",
        body=body,
    )
    return body
