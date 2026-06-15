from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


class LocalPreviewRouteSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalPreviewRouteSyncConfig:
    dynamic_dir: Path
    container_runtime_command: str


@dataclass(frozen=True)
class RouteTarget:
    service_key: str
    host: str
    upstream_url: str


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _route_file_path(*, dynamic_dir: Path, release_id: str) -> Path:
    return dynamic_dir / f"mb-preview-{release_id[:8]}.yaml"


def _load_route_bindings(payload: dict[str, object]) -> list[dict[str, object]]:
    bindings = payload.get("route_bindings")
    if not isinstance(bindings, list) or not bindings:
        raise LocalPreviewRouteSyncError("Local preview route sync payload is missing route_bindings")
    normalized: list[dict[str, object]] = []
    for item in bindings:
        if isinstance(item, dict):
            normalized.append(dict(item))
    if not normalized:
        raise LocalPreviewRouteSyncError("Local preview route sync payload has no usable route_bindings")
    return normalized


def _docker_json_lines(
    *,
    container_runtime_command: str,
    args: list[str],
    subprocess_run_fn=subprocess.run,
) -> str:
    completed = subprocess_run_fn(
        [container_runtime_command, *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise LocalPreviewRouteSyncError(stderr or f"Docker command failed: {' '.join(args)}")
    return completed.stdout


def _container_details_by_service(
    *,
    application_uuid: str,
    container_runtime_command: str,
    subprocess_run_fn=subprocess.run,
) -> dict[str, dict[str, object]]:
    container_ids_raw = _docker_json_lines(
        container_runtime_command=container_runtime_command,
        args=["ps", "-q", "--filter", f"label=com.docker.compose.project={application_uuid}"],
        subprocess_run_fn=subprocess_run_fn,
    )
    container_ids = [item.strip() for item in container_ids_raw.splitlines() if item.strip()]
    if not container_ids:
        raise LocalPreviewRouteSyncError(f"No running containers were found for compose project '{application_uuid}'")
    inspect_raw = _docker_json_lines(
        container_runtime_command=container_runtime_command,
        args=["inspect", *container_ids],
        subprocess_run_fn=subprocess_run_fn,
    )
    try:
        payload = json.loads(inspect_raw)
    except json.JSONDecodeError as exc:
        raise LocalPreviewRouteSyncError("Docker inspect output was not valid JSON") from exc
    if not isinstance(payload, list):
        raise LocalPreviewRouteSyncError("Docker inspect output must be a list")
    service_map: dict[str, dict[str, object]] = {}
    for item in payload:
        details = _coerce_dict(item)
        labels = _coerce_dict(_coerce_dict(details.get("Config")).get("Labels"))
        service_key = _normalize_optional_string(labels.get("com.docker.compose.service"))
        if service_key is None:
            continue
        service_map[service_key] = details
    return service_map


def _target_for_route_binding(
    *,
    service_map: dict[str, dict[str, object]],
    route_binding: dict[str, object],
    allow_single_container_match: bool = False,
) -> RouteTarget:
    service_key = _normalize_optional_string(route_binding.get("service_key"))
    host = _normalize_optional_string(route_binding.get("host"))
    port = _normalize_optional_string(route_binding.get("port"))
    if service_key is None or host is None or port is None:
        raise LocalPreviewRouteSyncError("Route binding is missing service_key, host, or port")
    details = service_map.get(service_key)
    if details is None and allow_single_container_match and len(service_map) == 1:
        details = next(iter(service_map.values()))
    if details is None:
        raise LocalPreviewRouteSyncError(f"Running container for service '{service_key}' was not found")
    networks = _coerce_dict(_coerce_dict(details.get("NetworkSettings")).get("Networks"))
    coolify_network = _coerce_dict(networks.get("coolify"))
    ip_address = _normalize_optional_string(coolify_network.get("IPAddress"))
    if ip_address is None:
        raise LocalPreviewRouteSyncError(f"Service '{service_key}' is missing a coolify network address")
    return RouteTarget(
        service_key=service_key,
        host=host,
        upstream_url=f"http://{ip_address}:{port}",
    )


def _router_name(*, release_id: str, service_key: str) -> str:
    normalized_service = "".join(character if character.isalnum() else "-" for character in service_key.lower()).strip("-")
    return f"mb-preview-{release_id[:8]}-{normalized_service}"


def _build_proxy_payload(*, release_id: str, targets: list[RouteTarget]) -> dict[str, object]:
    routers: dict[str, object] = {}
    services: dict[str, object] = {}
    for target in targets:
        name = _router_name(release_id=release_id, service_key=target.service_key)
        routers[name] = {
            "entryPoints": ["http"],
            "rule": f"Host(`{target.host}`) && PathPrefix(`/`)",
            "service": name,
            "priority": 5000,
        }
        services[name] = {
            "loadBalancer": {
                "servers": [{"url": target.upstream_url}],
            }
        }
    return {"http": {"routers": routers, "services": services}}


def sync_local_preview_routes(
    *,
    config: LocalPreviewRouteSyncConfig,
    payload: dict[str, object],
    subprocess_run_fn=subprocess.run,
) -> dict[str, object]:
    release_id = _normalize_optional_string(payload.get("release_id"))
    action = _normalize_optional_string(payload.get("action"))
    if release_id is None:
        raise LocalPreviewRouteSyncError("Local preview route sync payload is missing release_id")
    if action not in {"upsert", "remove"}:
        raise LocalPreviewRouteSyncError("Local preview route sync payload action must be 'upsert' or 'remove'")
    config.dynamic_dir.mkdir(parents=True, exist_ok=True)
    route_file = _route_file_path(dynamic_dir=config.dynamic_dir, release_id=release_id)
    if action == "remove":
        route_file.unlink(missing_ok=True)
        return {"action": action, "route_file": str(route_file), "targets": []}
    application_uuid = _normalize_optional_string(payload.get("application_uuid"))
    if application_uuid is None:
        raise LocalPreviewRouteSyncError("Local preview route sync payload is missing application_uuid")
    route_bindings = _load_route_bindings(payload)
    service_map = _container_details_by_service(
        application_uuid=application_uuid,
        container_runtime_command=config.container_runtime_command,
        subprocess_run_fn=subprocess_run_fn,
    )
    targets = [
        _target_for_route_binding(
            service_map=service_map,
            route_binding=route_binding,
            allow_single_container_match=len(route_bindings) == 1,
        )
        for route_binding in route_bindings
    ]
    route_file.write_text(
        yaml.safe_dump(_build_proxy_payload(release_id=release_id, targets=targets), sort_keys=False),
        encoding="utf-8",
    )
    return {
        "action": action,
        "route_file": str(route_file),
        "targets": [
            {
                "service_key": target.service_key,
                "host": target.host,
                "upstream_url": target.upstream_url,
            }
            for target in targets
        ],
    }
