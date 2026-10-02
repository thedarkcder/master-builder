from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from orchestrator.core.config import get_settings
from orchestrator.core.project_app_planner import (
    ProjectAppNormalizedCandidate,
    ProjectAppPreScanCandidate,
    scan_repo_for_project_apps,
)
from orchestrator.core.runtime.invocation import (
    AgentInvocationContext,
    invoke_runtime_json,
)
from orchestrator.core.runtime.runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.storage.models import Project, Tenant


class DeploymentPlanService(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str
    kind: Literal["api", "website"]
    source_path: str
    build_strategy: Literal["dockerfile", "maven", "npm", "docker_compose"]
    compose_service: str
    container_port: int = Field(ge=1, le=65535)
    healthcheck: str | None = None
    depends_on: list[str] = Field(default_factory=list)

    @field_validator("key", "name", "source_path", "compose_service")
    @classmethod
    def normalize_required_string(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class DeploymentPlanRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_key: str
    visibility: Literal["public", "internal"]

    @field_validator("service_key")
    @classmethod
    def normalize_required_string(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class DeploymentPlanResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: Literal[
        "postgres",
        "mysql",
        "redis",
        "object_storage",
        "elasticsearch",
        "activemq",
        "kafka",
        "smtp",
    ]
    name: str
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("key", "kind", "name")
    @classmethod
    def normalize_required_string(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class DeploymentPlanVolume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    type: Literal["persistent", "file"] = "persistent"
    name: str
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("key", "name")
    @classmethod
    def normalize_required_string(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized


class DeploymentPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    services: list[DeploymentPlanService]
    routes: list[DeploymentPlanRoute]
    resources: list[DeploymentPlanResource] = Field(default_factory=list)
    volumes: list[DeploymentPlanVolume] = Field(default_factory=list)
    compose_raw: str

    @field_validator("name")
    @classmethod
    def normalize_required_string(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        return normalized

    @field_validator("compose_raw")
    @classmethod
    def normalize_compose_raw(cls, value: object) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("value is required")
        if "\\n" in normalized and "\n" not in normalized:
            normalized = normalized.replace("\\n", "\n").strip()
        return normalized

    @model_validator(mode="after")
    def validate_plan(self) -> "DeploymentPlan":
        service_keys = {service.key for service in self.services}
        resource_keys = {resource.key for resource in self.resources}
        volume_keys = {volume.key for volume in self.volumes}
        duplicate_resource_keys = sorted(
            service_keys.intersection(resource_keys | volume_keys)
        )
        if duplicate_resource_keys:
            raise ValueError(
                "deployment plan has resource/volume key(s) that duplicate service key(s): "
                f"{', '.join(duplicate_resource_keys)}"
            )
        duplicate_volume_resource_keys = sorted(resource_keys.intersection(volume_keys))
        if duplicate_volume_resource_keys:
            raise ValueError(
                "deployment plan has volume key(s) that duplicate resource key(s): "
                f"{', '.join(duplicate_volume_resource_keys)}"
            )
        dependency_keys = service_keys | resource_keys | volume_keys
        if not service_keys:
            raise ValueError("deployment plan requires at least one service")
        route_keys = [route.service_key for route in self.routes]
        duplicate_route_keys = sorted(
            {key for key in route_keys if route_keys.count(key) > 1}
        )
        if duplicate_route_keys:
            raise ValueError(
                f"deployment plan has duplicate route(s): {', '.join(duplicate_route_keys)}"
            )
        missing_route_keys = sorted(service_keys.difference(route_keys))
        if missing_route_keys:
            raise ValueError(
                f"deployment plan is missing route(s): {', '.join(missing_route_keys)}"
            )
        unknown_route_keys = sorted(set(route_keys).difference(service_keys))
        if unknown_route_keys:
            raise ValueError(
                f"deployment plan routes unknown service(s): {', '.join(unknown_route_keys)}"
            )
        for service in self.services:
            unknown_dependencies = sorted(
                {
                    dependency
                    for dependency in service.depends_on
                    if dependency not in dependency_keys
                }
            )
            if unknown_dependencies:
                raise ValueError(
                    f"service {service.key} depends on unknown service(s): {', '.join(unknown_dependencies)}"
                )
        try:
            compose_payload = yaml.safe_load(self.compose_raw) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"compose_raw is not valid YAML: {exc}") from exc
        if not isinstance(compose_payload, dict):
            raise ValueError("compose_raw must be a Docker Compose object")
        compose_services = compose_payload.get("services")
        if not isinstance(compose_services, dict) or not compose_services:
            raise ValueError("compose_raw must define services")
        missing_compose_services = sorted(
            {
                service.compose_service
                for service in self.services
                if service.compose_service not in compose_services
            }
        )
        if missing_compose_services:
            raise ValueError(
                "compose_raw is missing planned service(s): "
                f"{', '.join(missing_compose_services)}"
            )
        resource_compose_services = {
            str(resource.config.get("compose_service") or "").strip()
            for resource in self.resources
            if str(resource.config.get("compose_service") or "").strip()
        }
        allowed_compose_services = {
            service.compose_service for service in self.services
        } | resource_compose_services
        undeclared_compose_services = sorted(
            set(compose_services).difference(allowed_compose_services)
        )
        if undeclared_compose_services:
            raise ValueError(
                "compose_raw contains undeclared service(s): "
                f"{', '.join(undeclared_compose_services)}"
            )
        for service_name, service_config in compose_services.items():
            if not isinstance(service_config, dict):
                continue
            _validate_deployable_build_context(
                service_name=str(service_name), build_config=service_config.get("build")
            )
            _validate_no_host_source_mounts(
                service_name=str(service_name), volumes=service_config.get("volumes")
            )
        return self


class DeploymentPlannerResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment: DeploymentPlan


@dataclass(frozen=True)
class DeploymentPlanningResult:
    app: ProjectAppNormalizedCandidate
    plan: DeploymentPlan
    raw_planner_result_json: dict[str, object]


_DEPLOYMENT_PLANNER_SYSTEM_PROMPT = """\
You are the Master Builder deployment planner.
Return JSON only.

Your job is to produce one repo-level deployment plan for the selected branch/commit.
Do not treat infrastructure folders or Docker Compose folders as deployable products.
Identify application services across the repo, identify supporting resources from Docker Compose, and generate one Docker Compose file that runs the full system.

Return this strict JSON shape:
{
  "deployment": {
    "name": "production",
        "services": [
      {
        "key": "stable-dns-safe-service-key",
        "name": "Human service name",
        "kind": "api | website",
        "source_path": "repo-relative path",
        "build_strategy": "dockerfile | maven | npm | docker_compose",
        "compose_service": "docker-compose service name",
        "container_port": 8080,
        "healthcheck": "/health",
        "depends_on": ["resource-or-service-key"],
      }
    ],
    "routes": [
      {"service_key": "identity-api", "visibility": "public"},
      {"service_key": "worker-api", "visibility": "internal"}
    ],
    "resources": [
      {
        "key": "postgres",
        "kind": "postgres | mysql | redis | object_storage | elasticsearch | activemq | kafka | smtp",
        "name": "postgres",
        "config": {"compose_service": "postgres", "service_type": "postgres", "source": "deployment_planner"}
      }
    ],
    "volumes": [
      {
        "key": "postgres-data",
        "type": "persistent",
        "name": "postgres-data",
        "config": {"compose_volume": "postgres-data", "mount_path": "/var/lib/postgresql/data", "source": "deployment_planner"}
      }
    ],
    "compose_raw": "version: '3.9'\\nservices:\\n  ..."
  }
}

Rules:
- For Java Maven services, use build_strategy "maven" and generate a compose service that builds/runs that module.
- For React/Vite/Next services, use build_strategy "npm" and generate a compose service that builds/runs that module.
- The compose_raw artifact is committed to a Master Builder deployment branch and deployed by Coolify from Git.
- Do not use host-source bind mounts such as ./api:/workspace/api, ../web:/app, or /tmp/source:/app.
- Do not use remote Git build contexts. Use repository-relative build contexts such as ./api/service because Coolify clones the private repository through its GitHub App before building.
- If a service has no Dockerfile, use dockerfile_inline with a repository-relative build context. Do not mount source code into runtime containers.
- Do not reuse Dockerfiles whose FROM image is in a private registry or registry that requires authentication. Generate dockerfile_inline from public base images instead.
- Docker Compose interpolates dollar variables inside dockerfile_inline. Escape shell variables as $$VAR or $${VAR}; never emit raw $VAR or ${VAR} in dockerfile_inline.
- Do not wire generated runtime containers to cloud secret managers or external cloud backing services. The generated compose must run against the compose resources it declares.
- Existing Docker Compose services that are databases, queues, search, SMTP/mail capture, or object storage are resources, not application services.
- Existing Docker Compose named volumes are volumes, not resources.
- Admin/helper/diagnostic UI services such as database browsers, Elasticsearch browsers, dashboards, sample tools, or local developer utilities are not deployment resources and must not appear in services, resources, routes, or compose_raw.
- Deployment resources are infrastructure services only: databases, caches, queues, search, SMTP/mail capture, and object storage.
- Deployment volumes are persistent mounts only and must appear in "volumes", not "resources".
- Infrastructure resources are not public routes.
- Every service must have one route. Use "public" only for user/API-facing services; use "internal" for service-to-service only traffic.
- Public URLs are generated later from service keys; do not hard-code host ports as the product contract.
- Use internal Docker service names for dependencies.
"""


def _is_forbidden_build_context(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized:
        return False
    return "://" in normalized or normalized.startswith(("../", "/", "~"))


def _is_host_source_path(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip()
    if not normalized:
        return False
    return normalized.startswith(("./", "../", "/", "~"))


def _validate_deployable_build_context(
    *, service_name: str, build_config: object
) -> None:
    if build_config is None:
        return
    context: object
    if isinstance(build_config, str):
        context = build_config
    elif isinstance(build_config, dict):
        context = build_config.get("context")
    else:
        raise ValueError(f"compose service {service_name} has invalid build config")
    if _is_forbidden_build_context(context):
        raise ValueError(
            f"compose service {service_name} uses a non-repository build context; "
            "use a repository-relative context because Coolify clones the private repository before building"
        )
    if isinstance(build_config, dict):
        dockerfile_inline = build_config.get("dockerfile_inline")
        if isinstance(dockerfile_inline, str):
            _validate_no_unescaped_compose_variables(
                service_name=service_name,
                dockerfile_text=dockerfile_inline,
            )


_UNESCAPED_COMPOSE_VARIABLE_PATTERN = re.compile(
    r"(?<!\$)\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*[^}]*\})"
)


def _validate_no_unescaped_compose_variables(
    *, service_name: str, dockerfile_text: str
) -> None:
    if _UNESCAPED_COMPOSE_VARIABLE_PATTERN.search(dockerfile_text):
        raise ValueError(
            f"compose service {service_name} dockerfile_inline contains unescaped shell variables; "
            "escape variables as $$VAR or $${VAR} so Docker Compose does not interpolate them"
        )


_PUBLIC_BASE_IMAGE_REGISTRIES = {
    "docker.io",
    "ghcr.io",
    "quay.io",
    "mcr.microsoft.com",
    "public.ecr.aws",
    "registry.k8s.io",
    "docker.elastic.co",
}


_VITE_RUNTIME_DOCKERFILE_TEMPLATE = """\
FROM node:22-alpine AS build
WORKDIR /app
COPY package*.json ./
RUN npm ci --legacy-peer-deps
COPY . .
RUN npx vite build

FROM node:22-alpine
WORKDIR /app
RUN npm install -g serve
COPY --from=build /app/dist ./dist
EXPOSE {container_port}
CMD ["serve", "-s", "dist", "-l", "{container_port}"]
"""


def _registry_host_from_image(image: str) -> str | None:
    if "/" not in image:
        return None
    first_segment = image.split("/", maxsplit=1)[0].strip().lower()
    if not first_segment or "." not in first_segment and ":" not in first_segment:
        return None
    return first_segment


def _validate_public_base_images(
    *,
    service_name: str,
    dockerfile_text: str,
) -> None:
    for line in dockerfile_text.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("from "):
            continue
        image = stripped.split()[1]
        registry_host = _registry_host_from_image(image)
        if (
            registry_host is not None
            and registry_host not in _PUBLIC_BASE_IMAGE_REGISTRIES
        ):
            raise ValueError(
                f"compose service {service_name} uses base image {image!r} from unsupported/private registry "
                f"{registry_host!r}; generate a Dockerfile from public base images or configure registry credentials"
            )


def _dockerfile_path_for_build(
    *, checkout_path: Path, build_config: object
) -> Path | None:
    if not isinstance(build_config, dict):
        return None
    dockerfile_inline = build_config.get("dockerfile_inline")
    if isinstance(dockerfile_inline, str) and dockerfile_inline.strip():
        return None
    context = str(build_config.get("context") or ".").strip() or "."
    dockerfile = (
        str(build_config.get("dockerfile") or "Dockerfile").strip() or "Dockerfile"
    )
    return (checkout_path / context / dockerfile).resolve()


def _validate_build_dockerfiles(*, plan: DeploymentPlan, checkout_path: str) -> None:
    root = Path(checkout_path).resolve()
    compose_payload = yaml.safe_load(plan.compose_raw) or {}
    compose_services = compose_payload.get("services")
    if not isinstance(compose_services, dict):
        return
    for service_name, service_config in compose_services.items():
        if not isinstance(service_config, dict):
            continue
        build_config = service_config.get("build")
        if isinstance(build_config, dict):
            dockerfile_inline = build_config.get("dockerfile_inline")
            if isinstance(dockerfile_inline, str) and dockerfile_inline.strip():
                _validate_public_base_images(
                    service_name=str(service_name),
                    dockerfile_text=dockerfile_inline,
                )
                continue
        dockerfile_path = _dockerfile_path_for_build(
            checkout_path=root, build_config=build_config
        )
        if dockerfile_path is None:
            continue
        try:
            dockerfile_path.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"compose service {service_name} Dockerfile path escapes the repository"
            ) from exc
        if not dockerfile_path.exists():
            continue
        _validate_public_base_images(
            service_name=str(service_name),
            dockerfile_text=dockerfile_path.read_text(encoding="utf-8"),
        )


def _package_json_for_service(
    *, checkout_path: Path, service: DeploymentPlanService
) -> dict[str, object] | None:
    package_json_path = (checkout_path / service.source_path / "package.json").resolve()
    try:
        package_json_path.relative_to(checkout_path)
    except ValueError as exc:
        raise ValueError(
            f"service {service.key} source_path escapes the repository"
        ) from exc
    if not package_json_path.exists():
        return None
    try:
        payload = json.loads(package_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"service {service.key} package.json is not valid JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"service {service.key} package.json must contain an object")
    return payload


def _is_vite_service_package(package_json: dict[str, object]) -> bool:
    for dependency_bucket in ("dependencies", "devDependencies"):
        dependencies = package_json.get(dependency_bucket)
        if isinstance(dependencies, dict) and "vite" in dependencies:
            return True
    return False


def _normalize_vite_service_builds(
    *, plan: DeploymentPlan, checkout_path: str
) -> DeploymentPlan:
    root = Path(checkout_path).resolve()
    compose_payload = yaml.safe_load(plan.compose_raw) or {}
    compose_services = compose_payload.get("services")
    if not isinstance(compose_services, dict):
        return plan

    changed = False
    for service in plan.services:
        if service.build_strategy != "npm" or service.kind != "website":
            continue
        package_json = _package_json_for_service(checkout_path=root, service=service)
        if package_json is None or not _is_vite_service_package(package_json):
            continue
        service_config = compose_services.get(service.compose_service)
        if not isinstance(service_config, dict):
            continue
        build_config = service_config.get("build")
        if isinstance(build_config, str):
            build_config = {"context": build_config}
        elif isinstance(build_config, dict):
            build_config = dict(build_config)
        else:
            build_config = {}
        build_config["context"] = f"./{service.source_path}"
        build_config.pop("dockerfile", None)
        build_config["dockerfile_inline"] = _VITE_RUNTIME_DOCKERFILE_TEMPLATE.format(
            container_port=service.container_port
        )
        service_config["build"] = build_config
        changed = True

    if not changed:
        return plan
    normalized_payload = plan.model_dump()
    normalized_payload["compose_raw"] = yaml.safe_dump(compose_payload, sort_keys=False)
    return DeploymentPlan.model_validate(normalized_payload)


def _normalize_python_service_builds(
    *, plan: DeploymentPlan, checkout_path: str
) -> DeploymentPlan:
    root = Path(checkout_path).resolve()
    compose_payload = yaml.safe_load(plan.compose_raw) or {}
    compose_services = compose_payload.get("services")
    if not isinstance(compose_services, dict):
        return plan

    changed = False
    for service in plan.services:
        service_config = compose_services.get(service.compose_service)
        if not isinstance(service_config, dict):
            continue
        build_config = service_config.get("build")
        if not isinstance(build_config, dict):
            continue
        dockerfile_inline = build_config.get("dockerfile_inline")
        if not isinstance(dockerfile_inline, str) or not dockerfile_inline.strip():
            continue
        if not any(
            line.strip().lower().startswith("from python:")
            for line in dockerfile_inline.splitlines()
        ):
            continue
        service_root = (root / service.source_path).resolve()
        try:
            service_root.relative_to(root)
        except ValueError as exc:
            raise ValueError(
                f"service {service.key} source_path escapes the repository"
            ) from exc

        normalized = dockerfile_inline
        if (
            service_root / ".projectroot"
        ).exists() and "COPY .projectroot " not in normalized:
            copy_anchor = "COPY src/ /app/"
            if copy_anchor not in normalized:
                raise ValueError(
                    f"python service {service.key} requires .projectroot but generated Dockerfile "
                    "does not copy src/ into /app/"
                )
            normalized = normalized.replace(
                copy_anchor, "COPY .projectroot /app/.projectroot\n" + copy_anchor
            )
        if _python_service_uses_legacy_langchain_imports(service_root=service_root):
            normalized = _ensure_legacy_langchain_dependencies(
                dockerfile_inline=normalized
            )
        sklearn_model_version = _python_service_sklearn_model_version(
            service_root=service_root
        )
        if sklearn_model_version:
            normalized = _ensure_sklearn_model_runtime_dependencies(
                dockerfile_inline=normalized,
                sklearn_version=sklearn_model_version,
            )
        if (
            "curl -fsS" in yaml.safe_dump(service_config.get("healthcheck"))
            and " apt-get install " in normalized
        ):
            normalized = re.sub(
                r"(apt-get install -y(?: --no-install-recommends)?)(?![^\n]*\bcurl\b)([^\n]*)",
                r"\1 curl\2",
                normalized,
                count=1,
            )
        if normalized != dockerfile_inline:
            build_config["dockerfile_inline"] = normalized
            changed = True

    if not changed:
        return plan
    normalized_payload = plan.model_dump()
    normalized_payload["compose_raw"] = yaml.safe_dump(compose_payload, sort_keys=False)
    return DeploymentPlan.model_validate(normalized_payload)


def _python_service_uses_legacy_langchain_imports(*, service_root: Path) -> bool:
    source_root = service_root / "src"
    if not source_root.exists():
        return False
    legacy_patterns = (
        "from langchain.utilities import",
        "from langchain.agents.agent_toolkits import",
        "from langchain.chains import",
        "from langchain.prompts import",
    )
    for path in source_root.rglob("*.py"):
        try:
            source = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(pattern in source for pattern in legacy_patterns):
            return True
    return False


_PYTHON_PACKAGE_PIN_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9_.-]+)==(?P<version>\d+(?:\.\d+){1,3})"
)
_SKLEARN_PICKLE_VERSION_PATTERN = re.compile(
    rb"_sklearn_version.{0,32}?(\d+\.\d+\.\d+)", re.DOTALL
)


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _python_requirements_pin(*, service_root: Path, package_name: str) -> str | None:
    requirements_path = service_root / "requirements.txt"
    if not requirements_path.exists():
        return None
    try:
        requirements = requirements_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    normalized_name = package_name.lower()
    for match in _PYTHON_PACKAGE_PIN_PATTERN.finditer(requirements):
        if match.group("name").lower() == normalized_name:
            return match.group("version")
    return None


def _python_service_sklearn_model_version(*, service_root: Path) -> str | None:
    requirements_version = _python_requirements_pin(
        service_root=service_root, package_name="scikit-learn"
    )
    if not requirements_version:
        return None

    model_versions: set[str] = set()
    for path in service_root.rglob("*.sav"):
        try:
            sample = path.read_bytes()
        except OSError:
            continue
        for match in _SKLEARN_PICKLE_VERSION_PATTERN.finditer(sample):
            model_versions.add(match.group(1).decode("ascii"))
    if not model_versions:
        return None

    newest_model_version = max(model_versions, key=_version_tuple)
    if _version_tuple(newest_model_version) >= _version_tuple(requirements_version):
        return None
    return newest_model_version


def _ensure_python_package_override(
    *,
    dockerfile_inline: str,
    package_name: str,
    package_version: str,
) -> str:
    pinned_package = f"{package_name}=={package_version}"
    if pinned_package in dockerfile_inline:
        return dockerfile_inline
    install_command = (
        f"RUN pip install --no-cache-dir --force-reinstall {pinned_package}\n"
    )
    return _insert_after_last_pip_install(
        dockerfile_inline=dockerfile_inline, install_command=install_command
    )


def _ensure_sklearn_model_runtime_dependencies(
    *, dockerfile_inline: str, sklearn_version: str
) -> str:
    pinned_sklearn = f"scikit-learn=={sklearn_version}"
    if pinned_sklearn in dockerfile_inline and "numpy==1.26.4" in dockerfile_inline:
        return dockerfile_inline
    install_command = (
        "RUN pip install --no-cache-dir --force-reinstall "
        "numpy==1.26.4 "
        "scipy==1.11.4 "
        f"{pinned_sklearn}\n"
    )
    return _insert_after_last_pip_install(
        dockerfile_inline=dockerfile_inline, install_command=install_command
    )


def _insert_after_last_pip_install(
    *, dockerfile_inline: str, install_command: str
) -> str:
    lines = dockerfile_inline.splitlines()
    insert_index = None
    for index, line in enumerate(lines):
        if line.strip().startswith("RUN pip install"):
            insert_index = index + 1
    if insert_index is None:
        raise ValueError(
            "python service requires package normalization but generated Dockerfile has no pip install step"
        )
    lines.insert(insert_index, install_command.rstrip())
    normalized = "\n".join(lines)
    if dockerfile_inline.endswith("\n"):
        normalized += "\n"
    return normalized


def _ensure_legacy_langchain_dependencies(*, dockerfile_inline: str) -> str:
    if "langchain==0.2.17" in dockerfile_inline:
        return dockerfile_inline
    install_command = (
        "RUN pip install --no-cache-dir "
        "langchain==0.2.17 "
        "langchain-community==0.2.19 "
        "langchain-core==0.2.43 "
        "langchain-openai==0.1.25\n"
    )
    return _insert_after_last_pip_install(
        dockerfile_inline=dockerfile_inline, install_command=install_command
    )


_LOCALHOST_PORT_PATTERN = re.compile(r"localhost:(?P<port>\d{1,5})")
_COMPOSE_HEALTHCHECK_COMMANDS = {"CMD", "CMD-SHELL", "NONE"}
_GENERIC_VOLUME_KEYS = {"data", "db", "storage", "volume"}
_ELASTICSEARCH_RUNTIME_IMAGE = "bitnamilegacy/elasticsearch:8"


def _normalize_compose_scalar_token(value: object) -> str:
    normalized = str(value or "").strip().replace('\\"', '"')
    while (
        len(normalized) >= 2
        and normalized[0] == normalized[-1]
        and normalized[0] in {"'", '"'}
    ):
        normalized = normalized[1:-1].strip()
    return normalized


def _normalize_compose_healthchecks(*, plan: DeploymentPlan) -> DeploymentPlan:
    compose_payload = yaml.safe_load(plan.compose_raw) or {}
    compose_services = compose_payload.get("services")
    if not isinstance(compose_services, dict):
        return plan

    changed = False
    for service_name, service_config in compose_services.items():
        if not isinstance(service_config, dict):
            continue
        healthcheck = service_config.get("healthcheck")
        if not isinstance(healthcheck, dict):
            continue
        test = healthcheck.get("test")
        if isinstance(test, list):
            normalized_test = [_normalize_compose_scalar_token(part) for part in test]
            if not normalized_test:
                raise ValueError(
                    f"compose service {service_name} healthcheck.test must not be empty"
                )
            command = normalized_test[0]
            if command not in _COMPOSE_HEALTHCHECK_COMMANDS:
                raise ValueError(
                    f"compose service {service_name} healthcheck.test must start with "
                    "CMD, CMD-SHELL, or NONE"
                )
            if normalized_test != test:
                healthcheck["test"] = normalized_test
                changed = True

    if not changed:
        return plan
    normalized_payload = plan.model_dump()
    normalized_payload["compose_raw"] = yaml.safe_dump(compose_payload, sort_keys=False)
    return DeploymentPlan.model_validate(normalized_payload)


def _compose_volume_sources(volumes: object) -> list[str]:
    if not isinstance(volumes, list):
        return []
    sources: list[str] = []
    for volume in volumes:
        if isinstance(volume, str):
            source = volume.split(":", maxsplit=1)[0].strip()
            if source:
                sources.append(source)
        elif isinstance(volume, dict):
            source = str(volume.get("source") or "").strip()
            if source:
                sources.append(source)
    return sources


def _replace_compose_volume_source(
    *, volumes: object, old_name: str, new_name: str
) -> bool:
    if not isinstance(volumes, list):
        return False
    changed = False
    for index, volume in enumerate(volumes):
        if isinstance(volume, str):
            parts = volume.split(":", maxsplit=1)
            if parts[0].strip() == old_name:
                volumes[index] = (
                    f"{new_name}:{parts[1]}" if len(parts) == 2 else new_name
                )
                changed = True
        elif isinstance(volume, dict):
            source = str(volume.get("source") or "").strip()
            if source == old_name:
                volume["source"] = new_name
                changed = True
    return changed


def _normalize_plan_volume_names(*, plan: DeploymentPlan) -> DeploymentPlan:
    compose_payload = yaml.safe_load(plan.compose_raw) or {}
    compose_services = compose_payload.get("services")
    compose_volumes = compose_payload.get("volumes")
    if not isinstance(compose_services, dict) or not isinstance(compose_volumes, dict):
        return plan

    resource_service_by_name = {
        _resource_compose_service(resource): resource.key for resource in plan.resources
    }
    rename_map: dict[str, str] = {}
    for volume in plan.volumes:
        compose_volume = str(
            volume.config.get("compose_volume") or volume.name or volume.key
        ).strip()
        if not compose_volume:
            raise ValueError(
                f"deployment volume {volume.key} must declare a compose_volume"
            )
        normalized_key = volume.key.strip().lower()
        target_volume_name = volume.key
        owning_resources = sorted(
            resource_key
            for service_name, resource_key in resource_service_by_name.items()
            if compose_volume
            in _compose_volume_sources(
                compose_services.get(service_name, {}).get("volumes")
                if isinstance(compose_services.get(service_name), dict)
                else None
            )
        )
        if normalized_key in _GENERIC_VOLUME_KEYS and not owning_resources:
            raise ValueError(
                f"deployment volume {volume.key} uses a generic name but is not attached to a declared resource"
            )
        if len(owning_resources) > 1:
            raise ValueError(
                f"deployment volume {volume.key} is attached to multiple resources: {', '.join(owning_resources)}"
            )
        if normalized_key in _GENERIC_VOLUME_KEYS:
            target_volume_name = f"{owning_resources[0]}-data"
        if compose_volume != target_volume_name:
            rename_map[compose_volume] = target_volume_name

    if not rename_map:
        return plan

    target_names = set(compose_volumes)
    for old_name, new_name in rename_map.items():
        if old_name == new_name:
            continue
        if new_name in target_names:
            raise ValueError(
                f"deployment volume rename target already exists: {new_name}"
            )
        compose_volumes[new_name] = compose_volumes.pop(old_name, {})
        target_names.add(new_name)
        target_names.discard(old_name)
        for service_config in compose_services.values():
            if isinstance(service_config, dict):
                _replace_compose_volume_source(
                    volumes=service_config.get("volumes"),
                    old_name=old_name,
                    new_name=new_name,
                )

    normalized_payload = plan.model_dump()
    normalized_volumes: list[dict[str, object]] = []
    for volume_payload in normalized_payload["volumes"]:
        config = dict(volume_payload.get("config") or {})
        compose_volume = str(
            config.get("compose_volume")
            or volume_payload.get("name")
            or volume_payload.get("key")
        ).strip()
        new_name = rename_map.get(compose_volume)
        if new_name:
            volume_payload = dict(volume_payload)
            volume_payload["key"] = new_name
            volume_payload["name"] = new_name
            config["compose_volume"] = new_name
            volume_payload["config"] = config
        normalized_volumes.append(volume_payload)
    normalized_payload["volumes"] = normalized_volumes
    normalized_payload["compose_raw"] = yaml.safe_dump(compose_payload, sort_keys=False)
    return DeploymentPlan.model_validate(normalized_payload)


def _compose_environment_mapping(
    *, service_name: str, environment: object
) -> dict[str, str]:
    if environment is None:
        return {}
    if isinstance(environment, dict):
        return {
            str(key): "" if value is None else str(value)
            for key, value in environment.items()
        }
    if not isinstance(environment, list):
        raise ValueError(
            f"compose service {service_name} environment must be a mapping or list"
        )
    parsed: dict[str, str] = {}
    for entry in environment:
        if not isinstance(entry, str) or "=" not in entry:
            raise ValueError(
                f"compose service {service_name} environment entries must use KEY=VALUE"
            )
        key, value = entry.split("=", maxsplit=1)
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError(
                f"compose service {service_name} environment contains an empty key"
            )
        parsed[normalized_key] = value
    return parsed


def _resource_compose_service(resource: DeploymentPlanResource) -> str:
    configured_service = str(resource.config.get("compose_service") or "").strip()
    return configured_service or resource.key


def _resource_internal_alias(*, service_name: str) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else "-" for character in service_name
    ).strip("-")
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return f"mb-{normalized or 'resource'}"


def _normalize_resource_internal_aliases(*, plan: DeploymentPlan) -> DeploymentPlan:
    compose_payload = yaml.safe_load(plan.compose_raw) or {}
    compose_services = compose_payload.get("services")
    if not isinstance(compose_services, dict):
        return plan

    changed = False
    for resource in plan.resources:
        service_name = _resource_compose_service(resource)
        service_config = compose_services.get(service_name)
        if not isinstance(service_config, dict):
            continue
        alias = _resource_internal_alias(service_name=service_name)
        networks = service_config.get("networks")
        if networks is None:
            service_config["networks"] = {"default": {"aliases": [alias]}}
            changed = True
            continue
        if isinstance(networks, list):
            network_mapping: dict[str, object] = {
                str(name): {} for name in networks if str(name).strip()
            }
            network_mapping.setdefault("default", {})
            networks = network_mapping
            service_config["networks"] = networks
            changed = True
        if not isinstance(networks, dict):
            raise ValueError(
                f"compose service {service_name} networks must be a mapping or list"
            )
        default_network = networks.get("default")
        if default_network is None:
            default_network = {}
            networks["default"] = default_network
            changed = True
        if not isinstance(default_network, dict):
            raise ValueError(
                f"compose service {service_name} default network config must be a mapping"
            )
        aliases = default_network.get("aliases")
        if aliases is None:
            default_network["aliases"] = [alias]
            changed = True
            continue
        if not isinstance(aliases, list):
            raise ValueError(
                f"compose service {service_name} default network aliases must be a list"
            )
        if alias not in aliases:
            aliases.append(alias)
            changed = True

    if not changed:
        return plan
    normalized_payload = plan.model_dump()
    normalized_payload["compose_raw"] = yaml.safe_dump(compose_payload, sort_keys=False)
    return DeploymentPlan.model_validate(normalized_payload)


def _normalize_elasticsearch_resource_images(*, plan: DeploymentPlan) -> DeploymentPlan:
    elasticsearch_services = {
        _resource_compose_service(resource)
        for resource in plan.resources
        if resource.kind == "elasticsearch"
    }
    if not elasticsearch_services:
        return plan

    compose_payload = yaml.safe_load(plan.compose_raw) or {}
    compose_services = compose_payload.get("services")
    if not isinstance(compose_services, dict):
        return plan

    changed = False
    for service_name in elasticsearch_services:
        service_config = compose_services.get(service_name)
        if not isinstance(service_config, dict):
            raise ValueError(
                f"elasticsearch resource references missing compose service {service_name}"
            )
        image = str(service_config.get("image") or "").strip()
        if image.startswith("docker.elastic.co/"):
            service_config["image"] = _ELASTICSEARCH_RUNTIME_IMAGE
            changed = True
        environment = _compose_environment_mapping(
            service_name=service_name,
            environment=service_config.get("environment"),
        )
        required_environment = {
            "ELASTICSEARCH_ENABLE_SECURITY": "false",
            "ELASTICSEARCH_CLUSTER_NAME": "master-builder",
            "ELASTICSEARCH_NODE_NAME": service_name,
            "ELASTICSEARCH_HEAP_SIZE": "512m",
        }
        merged_environment = {**environment, **required_environment}
        if merged_environment != environment:
            service_config["environment"] = merged_environment
            changed = True
        required_healthcheck = {
            "test": [
                "CMD-SHELL",
                "curl -fsS http://localhost:9200/_cluster/health >/dev/null || exit 1",
            ],
            "interval": "20s",
            "timeout": "5s",
            "retries": 20,
            "start_period": "60s",
        }
        if service_config.get("healthcheck") != required_healthcheck:
            service_config["healthcheck"] = required_healthcheck
            changed = True

    if not changed:
        return plan
    normalized_payload = plan.model_dump()
    normalized_payload["compose_raw"] = yaml.safe_dump(compose_payload, sort_keys=False)
    return DeploymentPlan.model_validate(normalized_payload)


@dataclass(frozen=True)
class _MavenBuildScope:
    context_path: str
    module_path: str


_MAVEN_MODULE_PATTERN = re.compile(r"<module>\s*([^<]+?)\s*</module>")


def _pom_modules(pom_path: Path) -> set[str]:
    try:
        pom_text = pom_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {
        match.group(1).strip().replace("\\", "/")
        for match in _MAVEN_MODULE_PATTERN.finditer(pom_text)
    }


def _maven_build_scope(
    *, checkout_path: Path, service: DeploymentPlanService
) -> _MavenBuildScope:
    root = checkout_path.resolve()
    service_root = (root / service.source_path).resolve()
    try:
        service_root.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"service {service.key} source_path escapes the repository"
        ) from exc
    if not (service_root / "pom.xml").exists():
        raise ValueError(
            f"maven service {service.key} is missing pom.xml at {service.source_path}"
        )

    for parent in [service_root.parent, *service_root.parents]:
        if parent == service_root:
            continue
        try:
            parent.relative_to(root)
        except ValueError:
            break
        relative_module = service_root.relative_to(parent).as_posix()
        if relative_module in _pom_modules(parent / "pom.xml"):
            context_path = parent.relative_to(root).as_posix()
            return _MavenBuildScope(
                context_path="." if context_path == "." else context_path,
                module_path=relative_module,
            )
        if parent == root:
            break

    return _MavenBuildScope(context_path=service.source_path, module_path=".")


def _maven_runtime_dockerfile(
    *, service: DeploymentPlanService, scope: _MavenBuildScope
) -> str:
    module_path = shlex.quote(scope.module_path)
    module_prefix = "" if scope.module_path == "." else f"{scope.module_path}/"
    target_path = shlex.quote(f"/workspace/{module_prefix}target")
    return f"""\
FROM maven:3.9.9-eclipse-temurin-17 AS build
WORKDIR /workspace
COPY . .
RUN mvn -pl {module_path} -am -DskipTests -DskipDocker=true -Dmaven.test.skip=true package
RUN set -eu; find {target_path} -maxdepth 1 -type f -name '*.jar' ! -name 'original-*.jar' > /tmp/runtime-jars; test "$$(wc -l < /tmp/runtime-jars)" -eq 1 || {{ echo 'Maven deployment requires exactly one runtime JAR' >&2; exit 1; }}; cp "$$(cat /tmp/runtime-jars)" /tmp/app.jar

FROM eclipse-temurin:17-jre
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /tmp/app.jar /app/app.jar
EXPOSE {service.container_port}
ENTRYPOINT ["java","-jar","/app/app.jar"]
"""


def _normalize_maven_service_builds(
    *, plan: DeploymentPlan, checkout_path: str
) -> DeploymentPlan:
    root = Path(checkout_path).resolve()
    maven_services = [
        service for service in plan.services if service.build_strategy == "maven"
    ]
    if not maven_services:
        return plan
    try:
        compose_payload = yaml.safe_load(plan.compose_raw)
    except yaml.YAMLError:
        raise ValueError("Maven deployment requires valid Compose YAML") from None
    if not isinstance(compose_payload, dict) or not isinstance(
        compose_payload.get("services"), dict
    ):
        raise ValueError("Maven deployment requires a Compose services mapping")
    compose_services = compose_payload["services"]

    changed = False
    for service in maven_services:
        service_config = compose_services.get(service.compose_service)
        if not isinstance(service_config, dict):
            raise ValueError(
                f"maven service {service.key} references missing compose service {service.compose_service}"
            )
        build_config = service_config.get("build")
        if isinstance(build_config, str):
            build_config = {"context": build_config}
        elif isinstance(build_config, dict):
            build_config = dict(build_config)
        elif build_config is None:
            build_config = {}
        else:
            raise ValueError(
                f"maven service {service.key} build must be a mapping or context path"
            )

        scope = _maven_build_scope(checkout_path=root, service=service)
        expected_context = (
            "." if scope.context_path == "." else f"./{scope.context_path}"
        )
        expected_dockerfile = _maven_runtime_dockerfile(service=service, scope=scope)
        if (
            build_config.get("context") != expected_context
            or build_config.get("dockerfile_inline") != expected_dockerfile
        ):
            build_config["context"] = expected_context
            build_config.pop("dockerfile", None)
            build_config["dockerfile_inline"] = expected_dockerfile
            service_config["build"] = build_config
            changed = True

    if not changed:
        return plan
    normalized_payload = plan.model_dump()
    normalized_payload["compose_raw"] = yaml.safe_dump(compose_payload, sort_keys=False)
    return DeploymentPlan.model_validate(normalized_payload)


def _validate_no_host_source_mounts(*, service_name: str, volumes: object) -> None:
    if volumes is None:
        return
    if not isinstance(volumes, list):
        raise ValueError(f"compose service {service_name} volumes must be a list")
    for volume in volumes:
        if isinstance(volume, str):
            source = volume.split(":", maxsplit=1)[0].strip()
            if _is_host_source_path(source):
                raise ValueError(
                    f"compose service {service_name} uses a host-source bind mount; "
                    "deployment compose must build immutable service images"
                )
        elif isinstance(volume, dict):
            volume_type = str(volume.get("type") or "").strip().lower()
            source = volume.get("source")
            if volume_type == "bind" or _is_host_source_path(source):
                raise ValueError(
                    f"compose service {service_name} uses a host-source bind mount; "
                    "deployment compose must build immutable service images"
                )
        else:
            raise ValueError(f"compose service {service_name} has invalid volume entry")


def _candidate_prompt_json(candidate, *, checkout_path: str) -> dict[str, object]:  # noqa: ANN001
    payload = {
        "name": candidate.name,
        "source_path": candidate.source_path,
        "build_strategy": candidate.build_strategy,
        "detected_runtime": candidate.detected_runtime,
        "detected_language": candidate.detected_language,
        "detection_confidence": candidate.detection_confidence,
        "exposed_port": candidate.exposed_port,
        "healthcheck": candidate.healthcheck,
        "start_command": candidate.start_command,
        "services": [
            dict(service) for service in getattr(candidate, "services_json", ())
        ],
        "resources": [dict(resource) for resource in candidate.resources_json],
        "volumes": [dict(volume) for volume in getattr(candidate, "volumes_json", ())],
        "needs_generated_files": candidate.needs_generated_files,
    }
    if candidate.build_strategy == "dockerfile":
        payload["uses_private_base_image"] = _dockerfile_uses_private_base(
            candidate, checkout_path=checkout_path
        )
    return payload


def _path_has_marker(
    checkout_path: str,
    source_path: str,
    patterns: tuple[str, ...],
    markers: tuple[str, ...],
) -> bool:
    root = Path(checkout_path).resolve()
    candidate_root = root if source_path == "." else root / source_path
    if not candidate_root.exists() or not candidate_root.is_dir():
        return False
    for pattern in patterns:
        for path in candidate_root.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            lowered = text.lower()
            if any(marker in lowered for marker in markers):
                return True
    return False


def _maven_candidate_is_runnable(
    candidate: ProjectAppPreScanCandidate, *, checkout_path: str
) -> bool:
    source_path = str(candidate.source_path or "").strip() or "."
    if Path(checkout_path, source_path, "Dockerfile").exists():
        return True
    if _path_has_marker(
        checkout_path,
        source_path,
        ("pom.xml",),
        ("<packaging>pom</packaging>",),
    ):
        return False
    return _path_has_marker(
        checkout_path,
        source_path,
        (
            "pom.xml",
            "src/main/resources/application.properties",
            "src/main/resources/application.yml",
            "src/main/resources/application.yaml",
            "src/main/java/**/*.java",
        ),
        (
            "spring-boot-maven-plugin",
            "springbootapplication",
            "server.port",
            "spring.application.name",
        ),
    )


def _dockerfile_uses_private_base(
    candidate: ProjectAppPreScanCandidate, *, checkout_path: str
) -> bool:
    source_path = str(candidate.source_path or "").strip() or "."
    dockerfile = Path(checkout_path, source_path, "Dockerfile")
    try:
        text = dockerfile.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    match = re.search(r"(?mi)^\s*FROM\s+([^\s]+)", text)
    if match is None:
        return False
    image = match.group(1).strip().lower()
    if image.startswith(
        (
            "scratch",
            "alpine",
            "debian",
            "ubuntu",
            "python",
            "node",
            "nginx",
            "maven",
            "eclipse-temurin",
        )
    ):
        return False
    if image.startswith(("public.ecr.aws/", "docker.io/", "ghcr.io/")):
        return False
    registry = image.split("/", 1)[0]
    return "." in registry or ":" in registry


def _deployment_planner_candidates(
    candidates: tuple[ProjectAppPreScanCandidate, ...],
    *,
    checkout_path: str,
) -> tuple[ProjectAppPreScanCandidate, ...]:
    filtered: list[ProjectAppPreScanCandidate] = []
    for candidate in candidates:
        if candidate.build_strategy == "docker_compose":
            filtered.append(candidate)
            continue
        if candidate.detected_language == "java":
            if (
                candidate.detected_runtime == "spring_boot"
                and _maven_candidate_is_runnable(
                    candidate,
                    checkout_path=checkout_path,
                )
            ):
                filtered.append(candidate)
            continue
        if candidate.detected_language == "javascript":
            if candidate.detected_runtime in {"nextjs", "react", "vite"}:
                filtered.append(candidate)
            continue
        if candidate.build_strategy == "dockerfile":
            filtered.append(candidate)
    return tuple(filtered)


def _render_user_prompt(
    *,
    tenant: Tenant,
    project: Project,
    checkout_path: str,
    branch: str,
    commit_sha: str,
    pre_scan_candidates: tuple,
) -> str:
    payload = {
        "tenant_id": tenant.tenant_id,
        "project_id": project.project_id,
        "project_name": project.name,
        "github_repository": project.github_repository,
        "branch": branch,
        "commit_sha": commit_sha,
        "checkout_path": str(Path(checkout_path).resolve()),
        "deterministic_repo_evidence": [
            _candidate_prompt_json(candidate, checkout_path=checkout_path)
            for candidate in pre_scan_candidates
        ],
    }
    return json.dumps(payload, sort_keys=True, indent=2)


def _deployment_service(
    service: DeploymentPlanService, *, visibility: str
) -> dict[str, object]:
    service_type = "api" if service.kind == "api" else "website"
    service_record: dict[str, object] = {
        "key": service.key,
        "kind": service_type,
        "name": service.name,
        "source_path": service.source_path,
        "compose_service": service.compose_service,
        "build_strategy": service.build_strategy,
        "container_port": service.container_port,
        "public": visibility == "public",
        "config": {
            "source": "deployment_planner",
            "route_visibility": visibility,
        },
    }
    if service.healthcheck is not None:
        service_record["config"]["healthcheck"] = service.healthcheck
    if service.depends_on:
        service_record["config"]["depends_on"] = list(service.depends_on)
    return service_record


def _deployment_app_candidate(
    *,
    project: Project,
    plan: DeploymentPlan,
    analysis_source: str | None,
) -> ProjectAppNormalizedCandidate:
    route_visibility_by_service = {
        route.service_key: route.visibility for route in plan.routes
    }
    services = [
        _deployment_service(
            service, visibility=route_visibility_by_service[service.key]
        )
        for service in plan.services
    ]
    resources = [resource.model_dump(exclude_none=True) for resource in plan.resources]
    volumes = [volume.model_dump(exclude_none=True) for volume in plan.volumes]
    return ProjectAppNormalizedCandidate(
        name=str(project.name or plan.name or "production"),
        source_path=".",
        build_strategy="docker_compose",
        detected_runtime="compose",
        detected_language=None,
        detection_confidence=1.0,
        exposed_port=None,
        healthcheck=None,
        start_command=None,
        env_schema_json={},
        secret_schema_json={},
        deployment_config={
            "source_strategy": "docker_compose",
            "services": services,
            "resources": resources,
            "volumes": volumes,
            "backup_policies": [],
            "generated_compose_raw": plan.compose_raw,
            "deployment_plan": plan.model_dump(exclude_none=True),
        },
        analysis_source=analysis_source,
        needs_generated_files=False,
        resources_json=tuple(resources),
        services_json=tuple(services),
        volumes_json=tuple(volumes),
        env_json={},
        secret_json={},
        slug="production",
    )


def _allowed_deployment_service_source_paths(
    candidates: tuple[ProjectAppPreScanCandidate, ...],
) -> set[str]:
    allowed: set[str] = set()
    for candidate in candidates:
        if candidate.build_strategy != "docker_compose":
            allowed.add(str(candidate.source_path or "").strip() or ".")
        for service in getattr(candidate, "services_json", ()):
            if not isinstance(service, dict):
                continue
            source_path = str(service.get("source_path") or "").strip()
            if source_path:
                allowed.add(source_path)
    return allowed


def _validate_plan_service_sources(
    *,
    plan: DeploymentPlan,
    candidates: tuple[ProjectAppPreScanCandidate, ...],
) -> None:
    allowed_source_paths = _allowed_deployment_service_source_paths(candidates)
    undeclared_source_paths = sorted(
        {
            service.source_path
            for service in plan.services
            if service.source_path not in allowed_source_paths
        }
    )
    if undeclared_source_paths:
        raise ValueError(
            "deployment plan contains undeclared deployment service source_path(s): "
            f"{', '.join(undeclared_source_paths)}"
        )


def run_project_deployment_planning(
    *,
    tenant: Tenant,
    project: Project,
    checkout_path: str,
    branch: str,
    commit_sha: str,
    analysis_source: str | None = None,
    session=None,  # noqa: ANN001
    settings=None,  # noqa: ANN001
    workflow_id: str | None = None,
    operation_id: str | None = None,
    attempt_id: str | None = None,
    attempt_number: int | None = None,
    extra_on_log_line: Callable[[str, str], None] | None = None,
) -> DeploymentPlanningResult:
    normalized_checkout_path = str(Path(checkout_path or "").resolve())
    pre_scan_candidates = scan_repo_for_project_apps(
        checkout_path=normalized_checkout_path,
        analysis_source=analysis_source,
    )
    deployment_candidates = _deployment_planner_candidates(
        pre_scan_candidates,
        checkout_path=normalized_checkout_path,
    )
    active_settings = settings or get_settings()
    runtime = build_codex_runtime(session=session, settings=active_settings)
    try:
        runtime_payload = invoke_runtime_json(
            runtime=runtime,
            context=AgentInvocationContext(
                channel="system",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                command="project_deployment_planning",
                stage="project_deployment_planning",
                working_dir=normalized_checkout_path,
                workflow_id=workflow_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
                attempt=attempt_number,
                reasoning_effort="medium",
            ),
            system_prompt=_DEPLOYMENT_PLANNER_SYSTEM_PROMPT,
            user_prompt=_render_user_prompt(
                tenant=tenant,
                project=project,
                checkout_path=normalized_checkout_path,
                branch=branch,
                commit_sha=commit_sha,
                pre_scan_candidates=deployment_candidates,
            ),
            extra_on_log_line=extra_on_log_line,
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Project deployment planning failed: {exc}") from exc

    response = DeploymentPlannerResponse.model_validate(runtime_payload)
    _validate_plan_service_sources(
        plan=response.deployment, candidates=deployment_candidates
    )
    deployment_plan = _normalize_compose_healthchecks(plan=response.deployment)
    deployment_plan = _normalize_plan_volume_names(plan=deployment_plan)
    deployment_plan = _normalize_resource_internal_aliases(plan=deployment_plan)
    deployment_plan = _normalize_elasticsearch_resource_images(plan=deployment_plan)
    deployment_plan = _normalize_vite_service_builds(
        plan=deployment_plan,
        checkout_path=normalized_checkout_path,
    )
    deployment_plan = _normalize_python_service_builds(
        plan=deployment_plan,
        checkout_path=normalized_checkout_path,
    )
    deployment_plan = _normalize_maven_service_builds(
        plan=deployment_plan,
        checkout_path=normalized_checkout_path,
    )
    _validate_build_dockerfiles(
        plan=deployment_plan, checkout_path=normalized_checkout_path
    )
    return DeploymentPlanningResult(
        app=_deployment_app_candidate(
            project=project,
            plan=deployment_plan,
            analysis_source=analysis_source,
        ),
        plan=deployment_plan,
        raw_planner_result_json=dict(runtime_payload),
    )
