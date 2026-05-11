from __future__ import annotations

from dataclasses import dataclass
import yaml


@dataclass(frozen=True)
class CoolifyComposeNormalizationResult:
    compose_raw: str
    exposed_ports_by_service: dict[str, list[str]]


def normalize_compose_for_coolify(compose_raw: str) -> CoolifyComposeNormalizationResult:
    """Convert host-bound compose ports into internal exposes for Coolify routing."""

    normalized_raw = str(compose_raw or "").strip()
    if not normalized_raw:
        raise ValueError("Docker Compose content is required")

    parsed = yaml.safe_load(normalized_raw)
    if not isinstance(parsed, dict):
        raise ValueError("Docker Compose content must be a mapping")

    services = parsed.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("Docker Compose content must define services")

    exposed_ports_by_service: dict[str, list[str]] = {}
    for service_name, service_config in services.items():
        if not isinstance(service_name, str) or not isinstance(service_config, dict):
            continue
        exposed_ports = _extract_container_ports(service_config.pop("ports", None))
        existing_expose = _normalize_expose_entries(service_config.get("expose"))
        merged_expose = sorted({*existing_expose, *exposed_ports}, key=_port_sort_key)
        if merged_expose:
            service_config["expose"] = merged_expose
            exposed_ports_by_service[service_name] = merged_expose
        _normalize_temporal_bind_address(service_config)

    return CoolifyComposeNormalizationResult(
        compose_raw=yaml.safe_dump(parsed, sort_keys=False),
        exposed_ports_by_service=exposed_ports_by_service,
    )


def _normalize_temporal_bind_address(service_config: dict[str, object]) -> None:
    image = str(service_config.get("image") or "").strip().lower()
    if not image.startswith("temporalio/auto-setup"):
        return
    environment = _normalize_environment_mapping(service_config.get("environment"))
    environment.setdefault("BIND_ON_IP", "0.0.0.0")
    service_config["environment"] = environment


def _extract_container_ports(ports: object) -> list[str]:
    if ports is None:
        return []
    if not isinstance(ports, list):
        raise ValueError("Docker Compose service ports must be a list")
    extracted: list[str] = []
    for item in ports:
        port = _container_port_from_mapping(item)
        if port is not None:
            extracted.append(port)
    return extracted


def _container_port_from_mapping(item: object) -> str | None:
    if isinstance(item, int):
        return str(item)
    if isinstance(item, dict):
        target = item.get("target")
        if target is None:
            return None
        return _normalize_port_token(target)
    if not isinstance(item, str):
        return None

    value = item.strip()
    if not value:
        return None
    value = value.rsplit("/", maxsplit=1)[0]
    if ":" not in value:
        return _normalize_port_token(value)
    return _normalize_port_token(value.rsplit(":", maxsplit=1)[-1])


def _normalize_expose_entries(value: object) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Docker Compose service expose must be a list")
    normalized: list[str] = []
    for item in value:
        port = _normalize_port_token(item)
        if port:
            normalized.append(port)
    return normalized


def _normalize_port_token(value: object) -> str | None:
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return None
    return normalized.rsplit("/", maxsplit=1)[0]


def _normalize_environment_mapping(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {str(key): str(child) for key, child in value.items()}
    if isinstance(value, list):
        environment: dict[str, str] = {}
        for item in value:
            if isinstance(item, str) and "=" in item:
                key, child = item.split("=", 1)
                environment[key.strip()] = child.strip()
        return environment
    raise ValueError("Docker Compose service environment must be a mapping or list")


def _port_sort_key(value: str) -> tuple[int, str]:
    try:
        return (0, f"{int(value):08d}")
    except ValueError:
        return (1, value)
