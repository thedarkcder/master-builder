from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Literal, Sequence

import yaml
from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Project, ProjectApp, ProjectAppAnalysisRun, Tenant

_IGNORE_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "__pycache__",
    "target",
    "vendor",
}
_COMPOSE_FILENAMES = {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
_ANDROID_GRADLE_FILENAMES = {"build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"}
_PYTHON_DEFAULT_PORTS = {
    "django": 8000,
    "fastapi": 8000,
    "flask": 5000,
    "uvicorn": 8000,
    "gunicorn": 8000,
    "streamlit": 8501,
    "gradio": 7860,
}
_NODE_HINTS = (
    ("@remix-run/", "remix", "javascript", 3000, "npm run start"),
    ("next", "nextjs", "javascript", 3000, "npm run start"),
    ("nuxt", "nuxt", "javascript", 3000, "npm run start"),
    ("@nestjs/core", "nestjs", "typescript", 3000, "npm run start:prod"),
    ("express", "express", "javascript", 3000, "npm start"),
    ("astro", "astro", "typescript", 4321, "npm run start"),
    ("vite", "vite", "javascript", 4173, "npm run preview"),
    ("@sveltejs/kit", "sveltekit", "javascript", 3000, "npm run start"),
)
_JAVA_DEFAULT_PORT = 8080
_DEPLOYMENT_OWNED_APP_STATUSES = frozenset({"deploying", "live", "failed"})


class ProjectAppPlannerRuntimeApp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    source_path: str
    build_strategy: Literal["dockerfile", "docker_compose", "nixpacks"]
    port: int | None = Field(default=None, ge=1, le=65535)
    healthcheck: str | None = None
    resources: list[dict[str, object]] = Field(default_factory=list)
    volumes: list[dict[str, object]] = Field(default_factory=list)
    env: dict[str, object] = Field(default_factory=dict)
    secrets: dict[str, object] = Field(default_factory=dict)
    needs_generated_files: bool = False

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("name is required")
        return normalized

    @field_validator("healthcheck")
    @classmethod
    def normalize_healthcheck(cls, value: object) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    @field_validator("source_path")
    @classmethod
    def normalize_source_path(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("source_path is required")
        return _normalize_repo_relative_path(value)

    @model_validator(mode="after")
    def validate_deployment_resource_contract(self) -> "ProjectAppPlannerRuntimeApp":
        for index, resource in enumerate(self.resources):
            if not isinstance(resource, dict):
                continue
            kind = str(resource.get("kind") or "").strip().lower()
            if kind == "service":
                raise ValueError(f"apps.resources[{index}] is a service; use apps.services instead")
            if kind in {"file", "persistent", "persistent_volume", "volume"}:
                raise ValueError(f"apps.resources[{index}] is a volume; use apps.volumes instead")
        return self

    @field_validator("build_strategy")
    @classmethod
    def normalize_build_strategy(cls, value: str | None) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in {"dockerfile", "docker_compose", "nixpacks"}:
            raise ValueError("build_strategy must be dockerfile, docker_compose, or nixpacks")
        return normalized


class ProjectAppPlannerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apps: list[ProjectAppPlannerRuntimeApp] = Field(default_factory=list)


@dataclass(frozen=True)
class ProjectAppPreScanCandidate:
    name: str
    source_path: str
    build_strategy: str
    detected_runtime: str | None
    detected_language: str | None
    detection_confidence: float
    exposed_port: int | None
    healthcheck: str | None
    start_command: str | None
    env_schema_json: dict[str, object]
    secret_schema_json: dict[str, object]
    deployment_config: dict[str, object]
    analysis_source: str | None
    needs_generated_files: bool
    services_json: tuple[dict[str, object], ...] = ()
    resources_json: tuple[dict[str, object], ...] = ()
    volumes_json: tuple[dict[str, object], ...] = ()
    env_json: dict[str, object] = field(default_factory=dict)
    secret_json: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "env_json", dict(self.env_json or {}))
        object.__setattr__(self, "secret_json", dict(self.secret_json or {}))

    def to_result_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source_path": self.source_path,
            "build_strategy": self.build_strategy,
            "port": self.exposed_port,
            "healthcheck": self.healthcheck,
            "services": [dict(service) for service in self.services_json],
            "resources": [dict(resource) for resource in self.resources_json],
            "volumes": [dict(volume) for volume in self.volumes_json],
            "env": dict(self.env_json or {}),
            "secrets": dict(self.secret_json or {}),
            "needs_generated_files": self.needs_generated_files,
        }


@dataclass(frozen=True)
class ProjectAppNormalizedCandidate(ProjectAppPreScanCandidate):
    slug: str = ""

    def to_project_app_kwargs(self, *, tenant_id: str, project_id: str, now: datetime | None = None) -> dict[str, object]:
        timestamp = now or datetime.now(timezone.utc)
        return {
            "app_id": self._app_id(tenant_id=tenant_id, project_id=project_id, source_path=self.source_path),
            "tenant_id": tenant_id,
            "project_id": project_id,
            "name": self.name,
            "slug": self.slug,
            "source_path": self.source_path,
            "detection_confidence": self.detection_confidence,
            "detected_runtime": self.detected_runtime,
            "detected_language": self.detected_language,
            "analysis_source": self.analysis_source,
            "build_strategy": self.build_strategy,
            "exposed_port": self.exposed_port,
            "healthcheck": self.healthcheck,
            "start_command": self.start_command,
            "env_schema_json": dict(self.env_schema_json or {}),
            "secret_schema_json": dict(self.secret_schema_json or {}),
            "deployment_config": dict(self.deployment_config or {}),
            "status": self.status,
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    @property
    def status(self) -> str:
        return "needs_pr_merge" if self.needs_generated_files else "ready"

    @staticmethod
    def _app_id(*, tenant_id: str, project_id: str, source_path: str) -> str:
        raw = f"{tenant_id}:{project_id}:{source_path}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class ProjectAppAnalysisRunMetadata:
    tenant_id: str
    project_id: str
    checkout_path: str
    analysis_source: str | None
    planner_version: str | None
    pre_scan_count: int
    runtime_count: int
    normalized_count: int
    raw_planner_result_json: dict[str, object]


@dataclass(frozen=True)
class ProjectAppAnalysisResult:
    apps: tuple[ProjectAppNormalizedCandidate, ...]
    metadata: ProjectAppAnalysisRunMetadata


def _normalize_repo_relative_path(value: object) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        raise ValueError("source_path is required")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if normalized == ".":
        return "."
    path = PurePosixPath(normalized)
    if path.is_absolute():
        raise ValueError("source_path must be repo-relative")
    if any(part == ".." for part in path.parts):
        raise ValueError("source_path must not traverse outside the repository")
    compacted = str(path).strip("/")
    return compacted or "."


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return normalized or "app"


def _source_path_suffix(source_path: str) -> str:
    return hashlib.sha1(source_path.encode("utf-8")).hexdigest()[:8]


def _deployment_candidate_name(*, repo_root: Path, directory: Path, evidence: dict[str, object]) -> str:
    explicit_name = _normalize_optional_string(evidence.get("name"))
    if explicit_name is not None:
        return explicit_name
    package_name = evidence.get("package_name") if isinstance(evidence.get("package_name"), str) else None
    normalized_package_name = _normalize_optional_string(package_name)
    if normalized_package_name is not None:
        return normalized_package_name
    if directory == repo_root:
        return repo_root.name
    if directory.name.lower() in {"docker", "compose", "deployment", "deploy"} and directory.parent != repo_root.parent:
        return directory.parent.name
    return directory.name or repo_root.name


def _unique_slug(*, base_slug: str, source_path: str, used_slugs: set[str]) -> str:
    candidate = base_slug
    if candidate not in used_slugs:
        used_slugs.add(candidate)
        return candidate
    candidate = f"{base_slug}-{_source_path_suffix(source_path)}"
    if candidate not in used_slugs:
        used_slugs.add(candidate)
        return candidate
    suffix = 2
    while True:
        candidate = f"{base_slug}-{suffix}"
        if candidate not in used_slugs:
            used_slugs.add(candidate)
            return candidate
        suffix += 1


def _iter_repo_files(repo_root: Path) -> Sequence[Path]:
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(repo_root):
        dirnames[:] = sorted(
            dirname for dirname in dirnames if dirname not in _IGNORE_DIR_NAMES and not dirname.startswith(".")
        )
        current_path = Path(current_root)
        for filename in sorted(filenames):
            files.append(current_path / filename)
    return files


def _extract_port_from_text(text: str) -> int | None:
    match = re.search(r"\bEXPOSE\s+([0-9]{1,5})", text, flags=re.IGNORECASE)
    if match is None:
        return None
    port = int(match.group(1))
    if port < 1 or port > 65535:
        return None
    return port


def _extract_compose_port_mappings(service: dict[str, object]) -> tuple[dict[str, object], ...]:
    ports = service.get("ports")
    if not isinstance(ports, list):
        return ()
    mappings: list[dict[str, object]] = []
    for item in ports:
        raw = str(item or "").strip()
        if not raw:
            continue
        protocol = "tcp"
        if "/" in raw:
            raw, protocol = raw.split("/", 1)
            protocol = protocol.strip() or "tcp"
        parts = [part.strip() for part in raw.split(":") if part.strip()]
        if not parts:
            continue
        container_raw = parts[-1]
        host_raw = parts[-2] if len(parts) >= 2 else parts[-1]
        if not container_raw.isdigit() or not host_raw.isdigit():
            continue
        container_port = int(container_raw)
        host_port = int(host_raw)
        if 1 <= container_port <= 65535 and 1 <= host_port <= 65535:
            mappings.append(
                {
                    "host_port": host_port,
                    "container_port": container_port,
                    "protocol": protocol,
                }
            )
    return tuple(mappings)


def _extract_compose_port(service: dict[str, object]) -> int | None:
    for mapping in _extract_compose_port_mappings(service):
        port = mapping.get("container_port")
        if isinstance(port, int) and 1 <= port <= 65535:
            return port
    return None


def _compose_public_port(compose_payload: dict[str, object]) -> int | None:
    services = compose_payload.get("services") if isinstance(compose_payload.get("services"), dict) else {}
    if not isinstance(services, dict):
        return None
    preferred_markers = ("web", "ui", "frontend", "app", "admin", "temporal-ui", "console")
    candidates: list[tuple[int, int]] = []
    for service_name, raw_service in services.items():
        if not isinstance(raw_service, dict):
            continue
        normalized_name = str(service_name or "").strip().lower()
        image = str(raw_service.get("image") or "").strip().lower()
        marker_rank = 100
        for index, marker in enumerate(preferred_markers):
            if marker in normalized_name or marker in image:
                marker_rank = index
                break
        for mapping in _extract_compose_port_mappings(raw_service):
            host_port = mapping.get("host_port")
            container_port = mapping.get("container_port")
            if not isinstance(host_port, int) or not isinstance(container_port, int):
                continue
            http_rank = 0 if container_port in {80, 3000, 4200, 4173, 5000, 8000, 8080, 8088, 9001, 1358} else 50
            candidates.append((marker_rank + http_rank, host_port))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][1]


def _normalize_resource_key(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return normalized or "resource"


def _infer_compose_service_resource(service_name: object, service: dict[str, object]) -> tuple[str | None, str | None]:
    normalized_service_name = str(service_name or "").strip().lower()
    image = str(service.get("image") or "").strip().lower()
    admin_helper_markers = {
        "dejavu",
        "temporal",
        "temporal-ui",
        "pgadmin",
        "adminer",
        "kibana",
        "swagger-ui",
    }
    if any(marker in image or marker in normalized_service_name for marker in admin_helper_markers):
        return None, None
    if "postgres" in image or "postgis" in image:
        return "postgres", None
    if "mysql" in image:
        return "mysql", None
    if "mariadb" in image:
        return "mariadb", None
    if "redis" in image:
        return "redis", None
    if "clickhouse" in image:
        return "clickhouse", None
    if "mongo" in image:
        return "mongodb", None
    if "minio" in image:
        return "object_storage", "minio"
    resource_type_markers = {
        "activemq": "activemq",
        "kafka": "kafka",
        "elasticsearch": "elasticsearch",
        "mailpit": "smtp",
    }
    for marker, resource_type in resource_type_markers.items():
        if marker in image or marker in normalized_service_name:
            return resource_type, marker

    service_type_markers = {
        "rabbitmq": "rabbitmq",
        "nats": "nats",
    }
    for marker, service_type in service_type_markers.items():
        if marker in image or marker in normalized_service_name:
            return "service", service_type
    return "service", None


def _compose_volume_mount_path(service: dict[str, object], volume_name: str) -> str | None:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return None
    for item in volumes:
        if not isinstance(item, str):
            continue
        parts = item.split(":")
        if len(parts) < 2:
            continue
        if parts[0].strip() == volume_name:
            return parts[1].strip() or None
    return None


def _extract_compose_resources(compose_payload: dict[str, object]) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    resources: list[dict[str, object]] = []
    volumes_json: list[dict[str, object]] = []
    resource_keys: set[str] = set()
    volume_keys: set[str] = set()
    services = compose_payload.get("services") if isinstance(compose_payload.get("services"), dict) else {}
    if isinstance(services, dict):
        for service_name, raw_service in sorted(services.items()):
            if not isinstance(raw_service, dict):
                continue
            kind, service_type = _infer_compose_service_resource(service_name, raw_service)
            if kind is None:
                continue
            port_mappings = _extract_compose_port_mappings(raw_service)
            if kind == "service" and service_type is None:
                has_http_port = any(
                    mapping.get("container_port") in {80, 3000, 4200, 4173, 5000, 8000, 8080, 8088, 9001, 1358}
                    for mapping in port_mappings
                )
                if not has_http_port:
                    continue
                service_type = "website"
            key = _normalize_resource_key(service_name)
            if key in resource_keys:
                continue
            resource_keys.add(key)
            config: dict[str, object] = {
                "compose_service": str(service_name),
                "source": "docker_compose",
            }
            image = _normalize_optional_string(raw_service.get("image"))
            if image is not None:
                config["image"] = image
            if service_type is not None:
                config["service_type"] = service_type
            if port_mappings:
                config["ports"] = [dict(mapping) for mapping in port_mappings]
            resources.append(
                {
                    "key": key,
                    "kind": kind,
                    "name": str(service_name),
                    "config": config,
                }
            )
    volumes = compose_payload.get("volumes") if isinstance(compose_payload.get("volumes"), dict) else {}
    if isinstance(volumes, dict):
        for volume_name in sorted(volumes):
            key = _normalize_resource_key(volume_name)
            if key in resource_keys or key in volume_keys:
                continue
            volume_keys.add(key)
            mount_path = None
            if isinstance(services, dict):
                for raw_service in services.values():
                    if isinstance(raw_service, dict):
                        mount_path = _compose_volume_mount_path(raw_service, str(volume_name))
                        if mount_path:
                            break
            config: dict[str, object] = {
                "compose_volume": str(volume_name),
                "source": "docker_compose",
            }
            if mount_path is not None:
                config["mount_path"] = mount_path
            volumes_json.append(
                {
                    "key": key,
                    "type": "persistent",
                    "name": str(volume_name),
                    "config": config,
                }
            )
    return tuple(resources), tuple(volumes_json)


def _service_record_from_resource(resource: dict[str, object]) -> dict[str, object]:
    config = dict(resource.get("config")) if isinstance(resource.get("config"), dict) else {}
    raw_service_type = str(config.get("service_type") or "").strip().lower()
    service_kind = "api" if raw_service_type == "api" else "website"
    service_record: dict[str, object] = {
        "key": resource.get("key"),
        "kind": service_kind,
        "name": resource.get("name"),
        "source_path": config.get("source_path"),
        "compose_service": config.get("compose_service") or resource.get("key"),
        "build_strategy": config.get("build_strategy"),
        "container_port": config.get("container_port")
        or config.get("target_port")
        or config.get("exposed_port")
        or config.get("port"),
        "public": config.get("public") is not False,
        "config": {
            key: value
            for key, value in config.items()
            if key
            not in {
                "service_type",
                "source_path",
                "compose_service",
                "build_strategy",
                "container_port",
                "target_port",
                "exposed_port",
                "port",
                "public",
            }
        },
    }
    return {key: value for key, value in service_record.items() if value is not None}


def _framework_hint_from_text(text: str) -> tuple[str | None, str | None, int | None, str | None, float]:
    lowered = text.lower()
    for marker, runtime, language, port, start_command in _NODE_HINTS:
        if marker in lowered:
            return runtime, language, port, start_command, 0.85
    if "django" in lowered:
        return "python", "python", _PYTHON_DEFAULT_PORTS["django"], "python manage.py runserver 0.0.0.0:8000", 0.8
    if "fastapi" in lowered:
        return "python", "python", _PYTHON_DEFAULT_PORTS["fastapi"], "uvicorn app.main:app --host 0.0.0.0 --port 8000", 0.8
    if "flask" in lowered:
        return "python", "python", _PYTHON_DEFAULT_PORTS["flask"], "flask run --host 0.0.0.0 --port 5000", 0.8
    if "streamlit" in lowered:
        return "python", "python", _PYTHON_DEFAULT_PORTS["streamlit"], "streamlit run app.py --server.address 0.0.0.0", 0.8
    if "go.mod" in lowered or "package main" in lowered:
        return "go", "go", 8080, "go run .", 0.7
    if "cargo" in lowered or "[package]" in lowered:
        return "rust", "rust", 8080, "cargo run", 0.7
    if "rails" in lowered or "gem " in lowered:
        return "ruby", "ruby", 3000, "bin/rails server -b 0.0.0.0 -p 3000", 0.7
    return None, None, None, None, 0.0


def _normalize_package_json(text: str) -> tuple[str | None, str | None, int | None, str | None, str | None, float]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None, None, None, None, None, 0.0
    if not isinstance(payload, dict):
        return None, None, None, None, None, 0.0
    package_name = _normalize_optional_string(payload.get("name"))
    scripts = payload.get("scripts") if isinstance(payload.get("scripts"), dict) else {}
    start_command = None
    if isinstance(scripts, dict):
        if "start" in scripts:
            start_command = "npm run start"
        elif "start:prod" in scripts:
            start_command = "npm run start:prod"
    dependencies: dict[str, object] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        raw = payload.get(key)
        if isinstance(raw, dict):
            dependencies.update(raw)
    lowered_dependencies = {str(key).lower() for key in dependencies}
    for marker, runtime, language, port, default_start in _NODE_HINTS:
        if any(marker in dependency for dependency in lowered_dependencies):
            return package_name, runtime, language, port, start_command or default_start, 0.85
    if "react-scripts" in lowered_dependencies:
        return package_name, "react", "javascript", 3000, start_command or "npm start", 0.75
    return package_name, None, None, None, start_command, 0.25 if package_name else 0.0


def _normalize_requirements_text(text: str) -> tuple[str | None, str | None, int | None, str | None, float]:
    lowered = text.lower()
    for framework, port in _PYTHON_DEFAULT_PORTS.items():
        if framework in lowered:
            if framework == "django":
                return "python", "python", port, "python manage.py runserver 0.0.0.0:8000", 0.75
            if framework == "fastapi":
                return "python", "python", port, "uvicorn app.main:app --host 0.0.0.0 --port 8000", 0.75
            if framework == "flask":
                return "python", "python", port, "flask run --host 0.0.0.0 --port 5000", 0.7
            if framework == "streamlit":
                return "python", "python", port, "streamlit run app.py --server.address 0.0.0.0", 0.7
    return None, None, None, None, 0.0


def _normalize_pom_text(text: str) -> tuple[str | None, str | None, int | None, str | None, float]:
    lowered = text.lower()
    if "<project" not in lowered:
        return None, None, None, None, 0.0
    runtime = "java"
    if "spring-boot" in lowered or "springframework.boot" in lowered:
        runtime = "spring_boot"
    return runtime, "java", _JAVA_DEFAULT_PORT, "java -jar target/*.jar", 0.8


def _compose_healthcheck(healthcheck: object) -> str | None:
    if healthcheck is None:
        return None
    if isinstance(healthcheck, str):
        normalized = healthcheck.strip()
        return normalized or None
    try:
        return json.dumps(healthcheck, sort_keys=True)
    except TypeError:
        return None


def _build_env_schema_from_env_file(content: str) -> dict[str, object]:
    schema: dict[str, object] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if not key:
            continue
        schema[key] = {"required": True, "source": ".env.example"}
    return schema


def _merge_schema(target: dict[str, object], source: dict[str, object]) -> dict[str, object]:
    merged = dict(target)
    for key, value in source.items():
        if key not in merged:
            merged[key] = value
    return merged


def _build_candidate_from_directory(*, repo_root: Path, directory: Path, evidence: dict[str, object]) -> ProjectAppPreScanCandidate | None:
    rel_dir = directory.relative_to(repo_root).as_posix() if directory != repo_root else "."
    rel_dir = _normalize_repo_relative_path(rel_dir)
    name = _deployment_candidate_name(repo_root=repo_root, directory=directory, evidence=evidence)

    build_strategy = "nixpacks"
    detected_runtime = None
    detected_language = None
    detection_confidence = 0.2
    exposed_port = None
    healthcheck = None
    start_command = None
    env_schema_json: dict[str, object] = {}
    secret_schema_json: dict[str, object] = {}
    deployment_config: dict[str, object] = {}
    services_json: tuple[dict[str, object], ...] = ()
    resources_json: tuple[dict[str, object], ...] = ()
    volumes_json: tuple[dict[str, object], ...] = ()
    env_json: dict[str, object] = {}
    secret_json: dict[str, object] = {}
    needs_generated_files = True
    has_deployable_evidence = False

    dockerfile_text = evidence.get("dockerfile_text")
    if isinstance(dockerfile_text, str):
        has_deployable_evidence = True
        build_strategy = "dockerfile"
        detected_runtime, detected_language, default_port, default_start, confidence = _framework_hint_from_text(dockerfile_text)
        detection_confidence = max(detection_confidence, 0.9)
        exposed_port = _extract_port_from_text(dockerfile_text) or default_port
        healthcheck = _compose_healthcheck(_extract_healthcheck_from_dockerfile(dockerfile_text))
        start_command = _extract_cmd_from_dockerfile(dockerfile_text) or default_start
        env_schema_json = _merge_schema(env_schema_json, _extract_env_schema_from_dockerfile(dockerfile_text))
        needs_generated_files = False
    compose_payload = evidence.get("compose_payload")
    if isinstance(compose_payload, dict):
        has_deployable_evidence = True
        build_strategy = "docker_compose"
        services = compose_payload.get("services") if isinstance(compose_payload.get("services"), dict) else {}
        first_service = next(iter(services.values()), {}) if isinstance(services, dict) else {}
        if isinstance(first_service, dict):
            exposed_port = exposed_port or _extract_compose_port(first_service)
            healthcheck = healthcheck or _compose_healthcheck(first_service.get("healthcheck"))
            start_command = start_command or _normalize_optional_string(first_service.get("command"))
            env_schema_json = _merge_schema(env_schema_json, _extract_env_schema_from_compose(first_service))
        exposed_port = _compose_public_port(compose_payload) or exposed_port
        compose_resources, volumes_json = _extract_compose_resources(compose_payload)
        services_json = tuple(
            _service_record_from_resource(resource)
            for resource in compose_resources
            if str(resource.get("kind") or "").strip().lower() == "service"
        )
        resources_json = tuple(
            resource
            for resource in compose_resources
            if str(resource.get("kind") or "").strip().lower() != "service"
        )
        deployment_config = {
            **deployment_config,
            "source_strategy": "docker_compose",
            "services": [dict(service) for service in services_json],
            "resources": [dict(resource) for resource in resources_json],
            "volumes": [dict(volume) for volume in volumes_json],
            "backup_policies": [],
        }
        needs_generated_files = False
        detection_confidence = max(detection_confidence, 0.95)

    package_json_text = evidence.get("package_json_text")
    if isinstance(package_json_text, str):
        package_name, runtime, language, default_port, default_start, confidence = _normalize_package_json(package_json_text)
        if runtime is not None or default_start is not None:
            has_deployable_evidence = True
        if package_name:
            name = package_name
        if runtime is not None:
            detected_runtime = runtime
        if language is not None:
            detected_language = language
        if default_port is not None and exposed_port is None:
            exposed_port = default_port
        if default_start is not None and start_command is None:
            start_command = default_start
        detection_confidence = max(detection_confidence, confidence)
    pyproject_text = evidence.get("pyproject_text")
    if isinstance(pyproject_text, str):
        runtime, language, default_port, default_start, confidence = _normalize_requirements_text(pyproject_text)
        if runtime is not None or default_start is not None:
            has_deployable_evidence = True
        if runtime is not None:
            detected_runtime = detected_runtime or runtime
        if language is not None:
            detected_language = detected_language or language
        if default_port is not None and exposed_port is None:
            exposed_port = default_port
        if default_start is not None and start_command is None:
            start_command = default_start
        detection_confidence = max(detection_confidence, confidence)
    requirements_text = evidence.get("requirements_text")
    if isinstance(requirements_text, str):
        runtime, language, default_port, default_start, confidence = _normalize_requirements_text(requirements_text)
        if runtime is not None or default_start is not None:
            has_deployable_evidence = True
        if runtime is not None:
            detected_runtime = detected_runtime or runtime
        if language is not None:
            detected_language = detected_language or language
        if default_port is not None and exposed_port is None:
            exposed_port = default_port
        if default_start is not None and start_command is None:
            start_command = default_start
        detection_confidence = max(detection_confidence, confidence)
    go_mod_text = evidence.get("go_mod_text")
    if isinstance(go_mod_text, str):
        has_deployable_evidence = True
        detected_runtime = detected_runtime or "go"
        detected_language = detected_language or "go"
        exposed_port = exposed_port or 8080
        start_command = start_command or "go run ."
        detection_confidence = max(detection_confidence, 0.7)
    cargo_text = evidence.get("cargo_text")
    if isinstance(cargo_text, str):
        has_deployable_evidence = True
        detected_runtime = detected_runtime or "rust"
        detected_language = detected_language or "rust"
        exposed_port = exposed_port or 8080
        start_command = start_command or "cargo run"
        detection_confidence = max(detection_confidence, 0.7)
    gemfile_text = evidence.get("gemfile_text")
    if isinstance(gemfile_text, str):
        has_deployable_evidence = True
        detected_runtime = detected_runtime or "ruby"
        detected_language = detected_language or "ruby"
        exposed_port = exposed_port or 3000
        start_command = start_command or "bin/rails server -b 0.0.0.0 -p 3000"
        detection_confidence = max(detection_confidence, 0.7)
    pom_text = evidence.get("pom_text")
    if isinstance(pom_text, str):
        runtime, language, default_port, default_start, confidence = _normalize_pom_text(pom_text)
        if runtime is not None or default_start is not None:
            has_deployable_evidence = True
        if runtime is not None:
            detected_runtime = detected_runtime or runtime
        if language is not None:
            detected_language = detected_language or language
        if default_port is not None and exposed_port is None:
            exposed_port = default_port
        if default_start is not None and start_command is None:
            start_command = default_start
        detection_confidence = max(detection_confidence, confidence)

    if evidence.get("ios_project") is True:
        has_deployable_evidence = True
        detected_runtime = "ios"
        detected_language = "swift"
        build_strategy = "nixpacks"
        needs_generated_files = False
        detection_confidence = max(detection_confidence, 0.9)
        deployment_config = {
            **deployment_config,
            "mobile_platform": "ios",
            "capture_target": "ios",
        }
    if evidence.get("android_project") is True:
        has_deployable_evidence = True
        detected_runtime = "android"
        detected_language = str(evidence.get("android_language") or "kotlin")
        build_strategy = "nixpacks"
        needs_generated_files = False
        detection_confidence = max(detection_confidence, 0.9)
        deployment_config = {
            **deployment_config,
            "mobile_platform": "android",
            "capture_target": "android",
        }

    env_example_text = evidence.get("env_example_text")
    if isinstance(env_example_text, str):
        env_schema_json = _merge_schema(env_schema_json, _build_env_schema_from_env_file(env_example_text))
        secret_schema_json = _merge_schema(secret_schema_json, _build_secret_schema_from_env_file(env_example_text))
        detection_confidence = max(detection_confidence, 0.55)

    if not has_deployable_evidence:
        return None

    return ProjectAppPreScanCandidate(
        name=name,
        source_path=rel_dir,
        build_strategy=build_strategy,
        detected_runtime=detected_runtime,
        detected_language=detected_language,
        detection_confidence=detection_confidence,
        exposed_port=exposed_port,
        healthcheck=healthcheck,
        start_command=start_command,
        env_schema_json=env_schema_json,
        secret_schema_json=secret_schema_json,
        deployment_config=deployment_config,
        analysis_source=_normalize_optional_string(evidence.get("analysis_source")),
        needs_generated_files=needs_generated_files,
        services_json=services_json,
        resources_json=resources_json,
        volumes_json=volumes_json,
        env_json=env_json,
        secret_json=secret_json,
    )


def _extract_env_schema_from_dockerfile(text: str) -> dict[str, object]:
    schema: dict[str, object] = {}
    for match in re.finditer(r"(?mi)^\s*ENV\s+([A-Za-z_][A-Za-z0-9_]*)=", text):
        schema[match.group(1)] = {"required": False, "source": "dockerfile"}
    return schema


def _extract_cmd_from_dockerfile(text: str) -> str | None:
    match = re.search(r"(?mi)^\s*CMD\s+(.*)$", text)
    if match is None:
        return None
    return _normalize_optional_string(match.group(1))


def _extract_healthcheck_from_dockerfile(text: str) -> str | None:
    match = re.search(r"(?mi)^\s*HEALTHCHECK\s+(.*)$", text)
    if match is None:
        return None
    return match.group(1).strip() or None


def _extract_env_schema_from_compose(service: dict[str, object]) -> dict[str, object]:
    schema: dict[str, object] = {}
    environment = service.get("environment")
    if isinstance(environment, dict):
        for key, value in environment.items():
            schema[str(key)] = {"required": value is not None, "source": "docker_compose"}
    elif isinstance(environment, list):
        for item in environment:
            key = str(item or "").split("=", 1)[0].strip()
            if key:
                schema[key] = {"required": True, "source": "docker_compose"}
    return schema


def _build_secret_schema_from_env_file(content: str) -> dict[str, object]:
    schema: dict[str, object] = {}
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if not key:
            continue
        upper = key.upper()
        if any(token in upper for token in ("SECRET", "TOKEN", "PASSWORD", "KEY", "PRIVATE", "CREDENTIAL")):
            schema[key] = {"required": True, "source": ".env.example"}
    return schema


def _evidence_for_directory(
    evidence_by_dir: dict[str, dict[str, object]],
    *,
    repo_root: Path,
    directory: Path,
) -> dict[str, object]:
    rel_dir = directory.relative_to(repo_root).as_posix() if directory != repo_root else "."
    rel_dir = _normalize_repo_relative_path(rel_dir)
    return evidence_by_dir.setdefault(rel_dir, {})


def _android_manifest_source_dir(*, repo_root: Path, file_path: Path) -> Path:
    try:
        relative_parts = file_path.relative_to(repo_root).parts
    except ValueError:
        return file_path.parent
    if len(relative_parts) >= 4 and relative_parts[-3:] == ("src", "main", "AndroidManifest.xml"):
        return repo_root.joinpath(*relative_parts[:-3])
    return file_path.parent


def _scan_repo_for_evidence(repo_root: Path) -> dict[str, dict[str, object]]:
    evidence_by_dir: dict[str, dict[str, object]] = {}
    for file_path in _iter_repo_files(repo_root):
        evidence = _evidence_for_directory(evidence_by_dir, repo_root=repo_root, directory=file_path.parent)
        filename = file_path.name
        lowered = filename.lower()
        try:
            content = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = ""

        if lowered == "project.pbxproj" and file_path.parent.suffix.lower() == ".xcodeproj":
            ios_evidence = _evidence_for_directory(
                evidence_by_dir,
                repo_root=repo_root,
                directory=file_path.parent.parent,
            )
            ios_evidence["ios_project"] = True
            ios_evidence["name"] = ios_evidence.get("name") or file_path.parent.stem
        elif lowered in _ANDROID_GRADLE_FILENAMES:
            lowered_content = content.lower()
            if (
                "com.android.application" in lowered_content
                or "com.android.library" in lowered_content
                or "com.android.test" in lowered_content
                or (lowered.startswith("settings.gradle") and "include ':app'" in lowered_content)
            ):
                evidence["android_project"] = True
                if "kotlin" in lowered_content or "org.jetbrains.kotlin.android" in lowered_content:
                    evidence["android_language"] = "kotlin"
                elif "com.android" in lowered_content:
                    evidence["android_language"] = evidence.get("android_language") or "java"
                if not evidence.get("name"):
                    evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered == "androidmanifest.xml":
            android_evidence = _evidence_for_directory(
                evidence_by_dir,
                repo_root=repo_root,
                directory=_android_manifest_source_dir(repo_root=repo_root, file_path=file_path),
            )
            android_evidence["android_project"] = True
            android_evidence["android_language"] = android_evidence.get("android_language") or "kotlin"
            if not android_evidence.get("name"):
                android_evidence["name"] = file_path.parent.parent.parent.name if len(file_path.parts) >= 4 else file_path.parent.name
        elif lowered == "dockerfile":
            evidence["dockerfile_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered in _COMPOSE_FILENAMES:
            try:
                evidence["compose_payload"] = yaml.safe_load(content) or {}
            except yaml.YAMLError:
                evidence["compose_payload"] = {}
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered == "package.json":
            evidence["package_json_text"] = content
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict) and payload.get("name") and not evidence.get("name"):
                evidence["name"] = str(payload.get("name"))
            if isinstance(payload, dict):
                evidence["package_name"] = payload.get("name")
        elif lowered == "pyproject.toml":
            evidence["pyproject_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered == "requirements.txt":
            evidence["requirements_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered == "pom.xml":
            evidence["pom_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered == "go.mod":
            evidence["go_mod_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered == "cargo.toml":
            evidence["cargo_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered == "gemfile":
            evidence["gemfile_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
        elif lowered in {".env.example", ".env.sample", ".env.template"}:
            evidence["env_example_text"] = content
            if not evidence.get("name"):
                evidence["name"] = file_path.parent.name or repo_root.name
    return evidence_by_dir


def scan_repo_for_project_apps(
    *,
    checkout_path: str,
    analysis_source: str | None = None,
) -> tuple[ProjectAppPreScanCandidate, ...]:
    repo_root = Path(str(checkout_path or "")).resolve()
    if not repo_root.exists() or not repo_root.is_dir():
        raise ValueError(f"Checkout path does not exist or is not a directory: {repo_root}")

    evidence_by_dir = _scan_repo_for_evidence(repo_root)
    candidates: list[ProjectAppPreScanCandidate] = []
    for rel_dir in sorted(evidence_by_dir):
        candidate = _build_candidate_from_directory(
            repo_root=repo_root,
            directory=repo_root if rel_dir == "." else repo_root / rel_dir,
            evidence={**evidence_by_dir[rel_dir], "analysis_source": analysis_source},
        )
        if candidate is not None:
            candidates.append(candidate)

    if not candidates:
        candidates.append(
            ProjectAppPreScanCandidate(
                name=repo_root.name or "app",
                source_path=".",
                build_strategy="nixpacks",
                detected_runtime=None,
                detected_language=None,
                detection_confidence=0.2,
                exposed_port=None,
                healthcheck=None,
                start_command=None,
                env_schema_json={},
                secret_schema_json={},
                deployment_config={},
                analysis_source=analysis_source,
                needs_generated_files=True,
                resources_json=(),
                env_json={},
                secret_json={},
            )
        )
    return tuple(candidates)


def normalize_project_app_planner_output(
    *,
    pre_scan_candidates: Sequence[ProjectAppPreScanCandidate],
    runtime_payload: dict[str, object] | None,
    analysis_source: str | None,
) -> tuple[ProjectAppNormalizedCandidate, ...]:
    contract = ProjectAppPlannerResponse.model_validate(runtime_payload or {})
    pre_scan_by_source = {candidate.source_path: candidate for candidate in pre_scan_candidates}
    _validate_runtime_app_sources(contract=contract, pre_scan_by_source=pre_scan_by_source)
    merged: dict[str, ProjectAppNormalizedCandidate] = {}

    for runtime_app in contract.apps:
        source_path = runtime_app.source_path
        pre_scan = pre_scan_by_source.get(source_path)
        merged[source_path] = _merge_candidate(
            runtime_app=runtime_app,
            pre_scan=pre_scan,
            analysis_source=analysis_source,
        )

    for candidate in pre_scan_candidates:
        if candidate.source_path in merged:
            continue
        candidate_data = dict(candidate.__dict__)
        candidate_data["analysis_source"] = analysis_source or candidate.analysis_source
        candidate_data["slug"] = ""
        merged[candidate.source_path] = ProjectAppNormalizedCandidate(
            **candidate_data,
        )

    used_slugs: set[str] = set()
    normalized: list[ProjectAppNormalizedCandidate] = []
    for source_path in sorted(merged):
        candidate = merged[source_path]
        base_slug = _slugify(candidate.name or candidate.source_path)
        slug = _unique_slug(base_slug=base_slug, source_path=candidate.source_path, used_slugs=used_slugs)
        candidate_data = dict(candidate.__dict__)
        candidate_data["slug"] = slug
        normalized.append(
            ProjectAppNormalizedCandidate(
                **candidate_data,
            )
        )
    return tuple(normalized)


def _validate_runtime_app_sources(
    *,
    contract: ProjectAppPlannerResponse,
    pre_scan_by_source: dict[str, ProjectAppPreScanCandidate],
) -> None:
    unknown_sources = sorted(
        {runtime_app.source_path for runtime_app in contract.apps if runtime_app.source_path not in pre_scan_by_source}
    )
    if not unknown_sources:
        return
    allowed_sources = sorted(pre_scan_by_source)
    raise ValueError(
        "Project app planner returned undeclared source_path(s): "
        f"{', '.join(unknown_sources)}. Runtime app source_path must match deterministic pre-scan candidates: "
        f"{', '.join(allowed_sources) if allowed_sources else '<none>'}"
    )


def _merge_candidate(
    *,
    runtime_app: ProjectAppPlannerRuntimeApp,
    pre_scan: ProjectAppPreScanCandidate | None,
    analysis_source: str | None,
) -> ProjectAppNormalizedCandidate:
    name = runtime_app.name or (pre_scan.name if pre_scan is not None else runtime_app.source_path)
    build_strategy = runtime_app.build_strategy or (pre_scan.build_strategy if pre_scan is not None else "nixpacks")
    exposed_port = runtime_app.port if runtime_app.port is not None else (pre_scan.exposed_port if pre_scan is not None else None)
    healthcheck = runtime_app.healthcheck or (pre_scan.healthcheck if pre_scan is not None else None)
    start_command = pre_scan.start_command if pre_scan is not None else None
    env_schema_json = dict(pre_scan.env_schema_json if pre_scan is not None else {})
    secret_schema_json = dict(pre_scan.secret_schema_json if pre_scan is not None else {})
    deployment_config = dict(pre_scan.deployment_config if pre_scan is not None else {})
    detected_runtime = pre_scan.detected_runtime if pre_scan is not None else None
    detected_language = pre_scan.detected_language if pre_scan is not None else None
    detection_confidence = pre_scan.detection_confidence if pre_scan is not None else 0.5
    needs_generated_files = runtime_app.needs_generated_files or (pre_scan.needs_generated_files if pre_scan is not None else False)
    runtime_resources = tuple(dict(resource) for resource in runtime_app.resources if isinstance(resource, dict))
    runtime_volumes = tuple(dict(volume) for volume in runtime_app.volumes if isinstance(volume, dict))
    if runtime_resources:
        runtime_services = tuple(
            _service_record_from_resource(resource)
            for resource in runtime_resources
            if str(resource.get("kind") or "").strip().lower() == "service"
        )
        runtime_resources = tuple(
            resource
            for resource in runtime_resources
            if str(resource.get("kind") or "").strip().lower() != "service"
        )
        services_json = runtime_services
        resources_json = runtime_resources
        volumes_json = runtime_volumes
    elif runtime_volumes:
        services_json = ()
        resources_json = ()
        volumes_json = runtime_volumes
    elif pre_scan is not None:
        services_json = tuple(dict(service) for service in getattr(pre_scan, "services_json", ()))
        resources_json = tuple(dict(resource) for resource in pre_scan.resources_json)
        volumes_json = tuple(dict(volume) for volume in getattr(pre_scan, "volumes_json", ()))
    else:
        services_json = ()
        resources_json = ()
        volumes_json = ()

    if services_json or resources_json or volumes_json:
        deployment_config = {
            **deployment_config,
            "services": [dict(service) for service in services_json],
            "resources": [dict(resource) for resource in resources_json],
            "volumes": [dict(volume) for volume in volumes_json],
        }
    env_json = {str(key): value for key, value in runtime_app.env.items()}
    secret_json = {str(key): value for key, value in runtime_app.secrets.items()}

    if runtime_app.env:
        env_schema_json = _merge_schema(env_schema_json, {str(key): value for key, value in runtime_app.env.items()})
    if runtime_app.secrets:
        secret_schema_json = _merge_schema(secret_schema_json, {str(key): value for key, value in runtime_app.secrets.items()})

    return ProjectAppNormalizedCandidate(
        name=name,
        source_path=runtime_app.source_path,
        build_strategy=str(build_strategy),
        detected_runtime=detected_runtime,
        detected_language=detected_language,
        detection_confidence=detection_confidence,
        exposed_port=exposed_port,
        healthcheck=healthcheck,
        start_command=start_command,
        env_schema_json=env_schema_json,
        secret_schema_json=secret_schema_json,
        deployment_config=deployment_config,
        analysis_source=analysis_source or (pre_scan.analysis_source if pre_scan is not None else None),
        needs_generated_files=needs_generated_files,
        services_json=services_json,
        resources_json=resources_json,
        volumes_json=volumes_json,
        env_json=env_json,
        secret_json=secret_json,
    )


def _coerce_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def get_project_app_analysis_run(*, session: Session, analysis_run_id: str) -> ProjectAppAnalysisRun | None:
    normalized = str(analysis_run_id or "").strip()
    if not normalized:
        return None
    return session.get(ProjectAppAnalysisRun, normalized)


def ensure_project_app(session: Session, *, tenant_id: str, project_id: str, candidate: ProjectAppNormalizedCandidate) -> ProjectApp:
    existing = session.execute(
        select(ProjectApp).where(
            ProjectApp.tenant_id == tenant_id,
            ProjectApp.project_id == project_id,
            ProjectApp.source_path == candidate.source_path,
        )
    ).scalars().first()
    now = datetime.now(timezone.utc)
    if existing is None:
        app = ProjectApp(
            app_id=ProjectAppNormalizedCandidate._app_id(
                tenant_id=tenant_id,
                project_id=project_id,
                source_path=candidate.source_path,
            ),
            tenant_id=tenant_id,
            project_id=project_id,
            name=candidate.name,
            slug=candidate.slug,
            source_path=candidate.source_path,
            detection_confidence=candidate.detection_confidence,
            detected_runtime=candidate.detected_runtime,
            detected_language=candidate.detected_language,
            analysis_source=candidate.analysis_source,
            build_strategy=candidate.build_strategy,
            exposed_port=candidate.exposed_port,
            healthcheck=candidate.healthcheck,
            start_command=candidate.start_command,
            env_schema_json=dict(candidate.env_schema_json or {}),
            secret_schema_json=dict(candidate.secret_schema_json or {}),
            deployment_config=dict(candidate.deployment_config or {}),
            status=candidate.status,
            created_at=now,
            updated_at=now,
        )
        session.add(app)
        return app

    existing.detection_confidence = candidate.detection_confidence
    existing.detected_runtime = candidate.detected_runtime
    existing.detected_language = candidate.detected_language
    existing.analysis_source = candidate.analysis_source
    existing.build_strategy = candidate.build_strategy
    existing.exposed_port = candidate.exposed_port
    existing.healthcheck = candidate.healthcheck
    existing.start_command = candidate.start_command
    existing.env_schema_json = dict(candidate.env_schema_json or {})
    existing.secret_schema_json = dict(candidate.secret_schema_json or {})
    if candidate.deployment_config:
        existing.deployment_config = dict(candidate.deployment_config)
    if str(existing.status or "").strip().lower() not in _DEPLOYMENT_OWNED_APP_STATUSES:
        existing.status = candidate.status
    existing.updated_at = now
    return existing


def mark_project_app_analysis_run_running(
    *,
    session: Session,
    analysis_run_id: str,
    now: datetime | None = None,
) -> ProjectAppAnalysisRun:
    run = get_project_app_analysis_run(session=session, analysis_run_id=analysis_run_id)
    if run is None:
        raise ValueError(f"Project app analysis run '{analysis_run_id}' was not found")
    timestamp = now or datetime.now(timezone.utc)
    run.status = "running"
    run.started_at = timestamp
    run.updated_at = timestamp
    return run


def mark_project_app_analysis_run_completed(
    *,
    session: Session,
    analysis_run_id: str,
    result_payload: dict[str, object],
    now: datetime | None = None,
) -> ProjectAppAnalysisRun:
    run = get_project_app_analysis_run(session=session, analysis_run_id=analysis_run_id)
    if run is None:
        raise ValueError(f"Project app analysis run '{analysis_run_id}' was not found")
    timestamp = now or datetime.now(timezone.utc)
    run.status = "completed"
    run.result_payload = dict(result_payload)
    run.error = None
    run.completed_at = timestamp
    run.updated_at = timestamp
    return run


def mark_project_app_analysis_run_failed(
    *,
    session: Session,
    analysis_run_id: str,
    error: str,
    result_payload: dict[str, object] | None = None,
    now: datetime | None = None,
) -> ProjectAppAnalysisRun:
    run = get_project_app_analysis_run(session=session, analysis_run_id=analysis_run_id)
    if run is None:
        raise ValueError(f"Project app analysis run '{analysis_run_id}' was not found")
    timestamp = now or datetime.now(timezone.utc)
    run.status = "failed"
    if result_payload is not None:
        run.result_payload = dict(result_payload)
    run.error = error
    run.completed_at = timestamp
    run.updated_at = timestamp
    return run


def persist_project_app_analysis_result(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    analysis_run_id: str,
    apps: Sequence[ProjectAppNormalizedCandidate],
    raw_planner_result_json: dict[str, object],
    analysis_source: str | None,
    planner_version: str | None,
    now: datetime | None = None,
) -> ProjectAppAnalysisRun:
    tenant = session.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found")
    project = session.get(Project, project_id)
    if project is None or project.tenant_id != tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    run = get_project_app_analysis_run(session=session, analysis_run_id=analysis_run_id)
    if run is None or run.tenant_id != tenant_id or run.project_id != project_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project app analysis run not found")
    timestamp = now or datetime.now(timezone.utc)
    normalized_apps = [app.to_result_json() for app in apps]
    run.planner_version = planner_version or run.planner_version
    run.request_payload = {
        **dict(run.request_payload or {}),
        "analysis_source": analysis_source,
        "checkout_path": run.request_payload.get("checkout_path") if isinstance(run.request_payload, dict) else None,
        "planner_version": planner_version or run.planner_version,
    }
    run.result_payload = {
        "raw_planner_result_json": dict(raw_planner_result_json),
        "normalized_apps": normalized_apps,
        "analysis_source": analysis_source,
        "planner_version": planner_version or run.planner_version,
    }
    run.updated_at = timestamp
    for candidate in apps:
        ensure_project_app(session, tenant_id=tenant_id, project_id=project_id, candidate=candidate)
    session.flush()
    return run
