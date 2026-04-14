from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from sqlalchemy.orm import Session

from orchestrator.core.binding_resolution_service import resolve_project_binding_values
from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.install_registry_service import (
    INSTALL_KIND_FASTLANE,
    INSTALL_KIND_RAILWAY,
    INSTALL_KIND_SLACK,
    INSTALL_KIND_SUPABASE,
    normalize_install_kind,
)
from orchestrator.storage.models import Project, ProjectInstall

_SAFE_SUBPROCESS_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "PATH", "SHELL", "TMPDIR", "USER")


def _normalize_working_dir(config: dict[str, Any], *, repo_dir: Path) -> Path:
    raw_value = str(config.get("working_dir") or ".").strip() or "."
    base_dir = repo_dir.resolve()
    candidate = Path(raw_value)
    resolved = (base_dir / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    try:
        resolved.relative_to(base_dir)
    except ValueError as exc:
        raise ValueError(f"Install working_dir '{raw_value}' must stay within the run repository") from exc
    if not resolved.exists():
        raise ValueError(f"Install working_dir '{raw_value}' does not exist")
    return resolved


def _normalize_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def validate_install_definition(*, kind: str, config: dict[str, Any]) -> dict[str, Any]:
    normalized_kind = normalize_install_kind(kind)
    normalized_config = dict(config if isinstance(config, dict) else {})
    if normalized_kind == INSTALL_KIND_FASTLANE:
        platform = str(normalized_config.get("platform") or "").strip()
        lane = str(normalized_config.get("lane") or "").strip()
        if not platform or not lane:
            raise ValueError("fastlane_lane installs require non-empty 'platform' and 'lane'")
        return {
            "working_dir": str(normalized_config.get("working_dir") or ".").strip() or ".",
            "platform": platform,
            "lane": lane,
            "use_bundle_exec": bool(normalized_config.get("use_bundle_exec", True)),
        }
    if normalized_kind in {INSTALL_KIND_SUPABASE, INSTALL_KIND_RAILWAY}:
        action = str(normalized_config.get("action") or "").strip()
        if not action:
            raise ValueError(f"{normalized_kind} installs require non-empty 'action'")
        return {
            "working_dir": str(normalized_config.get("working_dir") or ".").strip() or ".",
            "action": action,
            "args": _normalize_string_list(normalized_config.get("args")),
        }
    if normalized_kind == INSTALL_KIND_SLACK:
        action = str(normalized_config.get("action") or "").strip().lower() or "post_message"
        if action != "post_message":
            raise ValueError("slack_action installs currently support only action='post_message'")
        fallback_message = str(normalized_config.get("message") or "").strip() or None
        return {
            "action": action,
            "message": fallback_message,
        }
    raise ValueError(f"Unsupported install kind '{kind}'")


def _build_install_command(*, kind: str, config: dict[str, Any]) -> list[str]:
    if kind == INSTALL_KIND_FASTLANE:
        prefix = ["bundle", "exec"] if bool(config.get("use_bundle_exec", True)) else []
        return [*prefix, "fastlane", str(config["platform"]), str(config["lane"])]
    if kind == INSTALL_KIND_SUPABASE:
        return ["supabase", str(config["action"]), *list(config.get("args") or [])]
    if kind == INSTALL_KIND_RAILWAY:
        return ["railway", str(config["action"]), *list(config.get("args") or [])]
    raise ValueError(f"Install kind '{kind}' does not map to a subprocess command")


def _build_subprocess_env(*, bindings: dict[str, str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _SAFE_SUBPROCESS_ENV_KEYS:
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
    env.update(bindings)
    return env


def _redact_install_output(value: str, *, bindings: dict[str, str]) -> str:
    redacted = str(value or "")
    for secret_value in bindings.values():
        if secret_value:
            redacted = redacted.replace(secret_value, "[REDACTED]")
    return redact_sensitive_text(redacted)


def _execute_subprocess_install(
    *,
    kind: str,
    config: dict[str, Any],
    repo_dir: Path,
    bindings: dict[str, str],
) -> dict[str, Any]:
    working_dir = _normalize_working_dir(config, repo_dir=repo_dir)
    command = _build_install_command(kind=kind, config=config)
    process = subprocess.run(  # noqa: S603
        command,
        cwd=str(working_dir),
        env=_build_subprocess_env(bindings=bindings),
        capture_output=True,
        text=True,
        check=False,
    )
    return {
        "ok": process.returncode == 0,
        "exit_code": process.returncode,
        "stdout": _redact_install_output(process.stdout, bindings=bindings),
        "stderr": _redact_install_output(process.stderr, bindings=bindings),
        "working_dir": str(working_dir),
        "command": command,
    }


def _execute_slack_install(
    *,
    config: dict[str, Any],
    bindings: dict[str, str],
    runtime_input: dict[str, Any] | None,
) -> dict[str, Any]:
    webhook_url = next((value for value in bindings.values() if value), "").strip()
    if not webhook_url:
        raise ValueError("slack_action installs require at least one resolvable webhook binding")
    message = str(((runtime_input or {}).get("message")) or config.get("message") or "").strip()
    if not message:
        raise ValueError("slack_action requires a message in runtime_input.message or install config")
    payload = json.dumps({"text": message}).encode("utf-8")
    request = urllib_request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", 200) or 200)
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "exit_code": exc.code,
            "stdout": "",
            "stderr": _redact_install_output(error_body, bindings=bindings),
            "status_code": exc.code,
        }
    return {
        "ok": 200 <= status_code < 300,
        "exit_code": 0 if 200 <= status_code < 300 else status_code,
        "stdout": _redact_install_output(body, bindings=bindings),
        "stderr": "",
        "status_code": status_code,
    }


def run_install(
    *,
    session: Session,
    settings,
    project: Project,
    install: ProjectInstall,
    repo_dir: Path,
    runtime_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if install.tenant_id != project.tenant_id or install.project_id != project.project_id:
        raise PermissionError("Install does not belong to the active project")
    if not install.enabled:
        raise ValueError(f"Install '{install.install_id}' is disabled")
    normalized_config = validate_install_definition(
        kind=install.kind,
        config=install.config_json if isinstance(install.config_json, dict) else {},
    )
    bindings = resolve_project_binding_values(
        session=session,
        project=project,
        tenant_id=project.tenant_id,
        encryption_key=str(getattr(settings, "secrets_encryption_key", "") or "").strip(),
        keys=list(install.binding_names_json or []),
    )
    if install.kind == INSTALL_KIND_SLACK:
        payload = _execute_slack_install(config=normalized_config, bindings=bindings, runtime_input=runtime_input)
    else:
        payload = _execute_subprocess_install(
            kind=install.kind,
            config=normalized_config,
            repo_dir=repo_dir,
            bindings=bindings,
        )
    return {
        "install_id": install.install_id,
        "kind": install.kind,
        "label": install.label,
        **payload,
    }
