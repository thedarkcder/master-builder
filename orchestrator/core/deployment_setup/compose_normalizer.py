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


def normalize_generated_compose_for_coolify(compose_raw: str) -> CoolifyComposeNormalizationResult:
    result = normalize_compose_for_coolify(compose_raw)
    parsed = yaml.safe_load(result.compose_raw)
    if not isinstance(parsed, dict):
        raise ValueError("Docker Compose content must be a mapping")
    services = parsed.get("services")
    if not isinstance(services, dict):
        raise ValueError("Docker Compose content must define services")
    _normalize_generated_internal_resource_hosts(services)
    for service_config in services.values():
        if not isinstance(service_config, dict):
            continue
        _strip_generated_proxy_labels(service_config)
        _normalize_generated_spring_healthcheck(service_config)
        build_config = service_config.get("build")
        if not isinstance(build_config, dict):
            continue
        dockerfile_inline = build_config.get("dockerfile_inline")
        if isinstance(dockerfile_inline, str) and "FROM node:" in dockerfile_inline:
            build_config["dockerfile_inline"] = _normalize_generated_node_dockerfile(dockerfile_inline)
    return CoolifyComposeNormalizationResult(
        compose_raw=yaml.safe_dump(parsed, sort_keys=False),
        exposed_ports_by_service=result.exposed_ports_by_service,
    )


def _normalize_generated_internal_resource_hosts(services: dict[object, object]) -> None:
    resource_aliases = {
        "postgres": "mb-postgres",
        "activemq": "mb-activemq",
        "kafka": "mb-kafka",
        "elasticsearch": "mb-elasticsearch",
        "mailpit": "mb-mailpit",
        "minio": "mb-minio",
    }
    for service_name, alias in resource_aliases.items():
        service_config = services.get(service_name)
        if isinstance(service_config, dict):
            _ensure_default_network_alias(service_config, alias)

    replacements = {
        "postgres:5432": "mb-postgres:5432",
        "activemq:61616": "mb-activemq:61616",
        "kafka:9092": "mb-kafka:9092",
        "elasticsearch:9200": "mb-elasticsearch:9200",
        "elasticsearch:9300": "mb-elasticsearch:9300",
        "mailpit:1025": "mb-mailpit:1025",
        "minio:9000": "mb-minio:9000",
    }
    exact_value_replacements = {
        "elasticsearch": "mb-elasticsearch",
        "mailpit": "mb-mailpit",
    }
    for service_config in services.values():
        if not isinstance(service_config, dict):
            continue
        environment = service_config.get("environment")
        if environment is None:
            continue
        normalized_environment = _normalize_environment_mapping(environment)
        changed = False
        for key, value in list(normalized_environment.items()):
            new_value = exact_value_replacements.get(value, value)
            for old, new in replacements.items():
                new_value = new_value.replace(old, new)
            if new_value != value:
                normalized_environment[key] = new_value
                changed = True
        if changed or not isinstance(environment, dict):
            service_config["environment"] = normalized_environment
        if any("mb-" in value for value in normalized_environment.values()):
            _ensure_default_network_membership(service_config)


def _ensure_default_network_alias(service_config: dict[str, object], alias: str) -> None:
    networks = service_config.get("networks")
    if networks is None:
        service_config["networks"] = {"default": {"aliases": [alias]}}
        return
    if isinstance(networks, list):
        networks = {str(network): {} for network in networks if str(network).strip()}
        service_config["networks"] = networks
    if not isinstance(networks, dict):
        raise ValueError("Docker Compose service networks must be a mapping or list")
    default_network = networks.setdefault("default", {})
    if not isinstance(default_network, dict):
        raise ValueError("Docker Compose default network config must be a mapping")
    aliases = default_network.setdefault("aliases", [])
    if not isinstance(aliases, list):
        raise ValueError("Docker Compose default network aliases must be a list")
    if alias not in aliases:
        aliases.append(alias)


def _ensure_default_network_membership(service_config: dict[str, object]) -> None:
    networks = service_config.get("networks")
    if networks is None:
        service_config["networks"] = {"default": {}}
        return
    if isinstance(networks, list):
        network_mapping = {str(network): {} for network in networks if str(network).strip()}
        network_mapping.setdefault("default", {})
        service_config["networks"] = network_mapping
        return
    if not isinstance(networks, dict):
        raise ValueError("Docker Compose service networks must be a mapping or list")
    networks.setdefault("default", {})


def _normalize_generated_node_dockerfile(dockerfile_inline: str) -> str:
    lines = dockerfile_inline.splitlines()
    normalized_lines = [
        "RUN npm ci --legacy-peer-deps" if line.strip() == "RUN npm ci" else line
        for line in lines
    ]
    suffix = "\n" if dockerfile_inline.endswith("\n") else ""
    return "\n".join(normalized_lines) + suffix


def _normalize_generated_spring_healthcheck(service_config: dict[str, object]) -> None:
    healthcheck = service_config.get("healthcheck")
    if not isinstance(healthcheck, dict):
        return
    test = healthcheck.get("test")
    if "actuator/health" not in str(test):
        return
    healthcheck["start_period"] = "600s"
    healthcheck["interval"] = "15s"
    healthcheck["timeout"] = "5s"
    healthcheck["retries"] = 40


def _strip_generated_proxy_labels(service_config: dict[str, object]) -> None:
    labels = service_config.get("labels")
    if labels is None:
        return

    if isinstance(labels, list):
        filtered_labels = [
            item
            for item in labels
            if not _is_generated_proxy_label(item)
        ]
        if filtered_labels:
            service_config["labels"] = filtered_labels
        else:
            service_config.pop("labels", None)
        return

    if not isinstance(labels, dict):
        raise ValueError("Docker Compose service labels must be a mapping or list")

    filtered_mapping = {
        str(key): value
        for key, value in labels.items()
        if not _is_generated_proxy_label(key)
    }
    if filtered_mapping:
        service_config["labels"] = filtered_mapping
    else:
        service_config.pop("labels", None)


def _is_generated_proxy_label(value: object) -> bool:
    normalized = str(value or "").strip().lower()
    return normalized.startswith("traefik.") or normalized.startswith("caddy_")


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
