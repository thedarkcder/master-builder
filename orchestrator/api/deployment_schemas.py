from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_serializer, model_validator

_DEPLOYMENT_VOLUME_KINDS = {"file", "persistent", "persistent_volume", "volume"}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_required_string(value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("value is required")
    return normalized


def _normalize_lower_string(value: object) -> str:
    normalized = _normalize_required_string(value)
    return normalized.lower()


def _normalize_string_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        normalized_key = _normalize_required_string(key)
        normalized_value = _normalize_required_string(value)
        normalized[normalized_key] = normalized_value
    return normalized


def _normalize_unique_lower_key_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw:
        normalized_value = _normalize_lower_string(value)
        if normalized_value in seen:
            continue
        seen.add(normalized_value)
        normalized.append(normalized_value)
    return normalized


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
    if not compacted:
        return "."
    return compacted


_SLUG_NORMALIZATION_PATTERN = re.compile(r"[^a-z0-9]+")
_RAW_SECRET_CONFIG_KEY_PARTS = {
    "password",
    "secret",
    "token",
    "private_key",
    "access_key",
}


def _is_raw_secret_config_key(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    if not normalized or normalized.endswith("_secret_ref") or normalized.endswith("_secret_refs"):
        return False
    if normalized == "secret_refs":
        return False
    parts = {part for part in re.split(r"[^a-z0-9]+", normalized) if part}
    if "secret" in parts and "ref" in parts:
        return False
    return bool(parts & _RAW_SECRET_CONFIG_KEY_PARTS)


def _assert_no_raw_secret_config(value: object, *, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_raw_secret_config_key(key):
                raise ValueError(f"{path}.{key} must be stored as a *_secret_ref, not raw secret material")
            _assert_no_raw_secret_config(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_raw_secret_config(child, path=f"{path}[{index}]")


def _redact_secret_config(value: object) -> object:
    if isinstance(value, dict):
        redacted: dict[str, object] = {}
        for key, child in value.items():
            if _is_raw_secret_config_key(key):
                continue
            normalized_key = str(key or "").strip().lower()
            if normalized_key == "secret_refs" or normalized_key.endswith("_secret_refs"):
                redacted[str(key)] = child
                continue
            redacted[str(key)] = _redact_secret_config(child)
        return redacted
    if isinstance(value, list):
        return [_redact_secret_config(child) for child in value]
    return value


def redact_deployment_config_secrets(value: object) -> dict[str, object]:
    redacted = _redact_secret_config(value)
    if isinstance(redacted, dict):
        return dict(redacted)
    return {}


def _normalize_app_slug(value: object) -> str:
    normalized = _normalize_required_string(value).lower()
    normalized = _SLUG_NORMALIZATION_PATTERN.sub("-", normalized).strip("-")
    if not normalized:
        raise ValueError("slug is required")
    return normalized




class ProjectAppCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    slug: str = Field(min_length=1, max_length=255)
    source_path: str = Field(default=".")
    detection_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detected_runtime: str | None = None
    detected_language: str | None = None
    analysis_source: str | None = None
    build_strategy: Literal["dockerfile", "docker_compose", "nixpacks"] | None = None
    exposed_port: int | None = Field(default=None, ge=1, le=65535)
    healthcheck: str | None = None
    start_command: str | None = None
    env_schema_json: dict[str, object] = Field(default_factory=dict)
    secret_schema_json: dict[str, object] = Field(default_factory=dict)
    deployment_config: ProjectDeploymentConfigWrite = Field(default_factory=lambda: ProjectDeploymentConfigWrite())

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, value: object) -> str:
        return _normalize_app_slug(value)

    @field_validator("source_path")
    @classmethod
    def normalize_source_path(cls, value: object) -> str:
        return _normalize_repo_relative_path(value)

    @field_validator("detected_runtime", "detected_language", "analysis_source", "build_strategy", "healthcheck", "start_command")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)


class ProjectAppUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=255)
    source_path: str | None = None
    detection_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    detected_runtime: str | None = None
    detected_language: str | None = None
    analysis_source: str | None = None
    build_strategy: Literal["dockerfile", "docker_compose", "nixpacks"] | None = None
    exposed_port: int | None = Field(default=None, ge=1, le=65535)
    healthcheck: str | None = None
    start_command: str | None = None
    env_schema_json: dict[str, object] | None = None
    secret_schema_json: dict[str, object] | None = None
    deployment_config: ProjectDeploymentConfigWrite | None = None

    @field_validator("slug")
    @classmethod
    def normalize_slug(cls, value: object) -> str | None:
        if value is None:
            return None
        return _normalize_app_slug(value)

    @field_validator("source_path")
    @classmethod
    def normalize_source_path(cls, value: object) -> str | None:
        if value is None:
            return None
        return _normalize_repo_relative_path(value)

    @field_validator("detected_runtime", "detected_language", "analysis_source", "build_strategy", "healthcheck", "start_command")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)


class ProjectAppRead(BaseModel):
    app_id: str
    tenant_id: str
    project_id: str
    name: str
    slug: str
    source_path: str
    detection_confidence: float | None = None
    detected_runtime: str | None = None
    detected_language: str | None = None
    analysis_source: str | None = None
    build_strategy: Literal["dockerfile", "docker_compose", "nixpacks"] | None = None
    exposed_port: int | None = None
    healthcheck: str | None = None
    start_command: str | None = None
    env_schema_json: dict[str, object] = Field(default_factory=dict)
    secret_schema_json: dict[str, object] = Field(default_factory=dict)
    deployment_config: ProjectDeploymentConfigRead = Field(default_factory=lambda: ProjectDeploymentConfigRead())
    status: Literal["draft", "needs_pr_merge", "ready", "deploying", "live", "failed"]
    latest_release_id: str | None = None
    latest_release_status: str | None = None
    latest_release_name: str | None = None
    latest_release_git_ref: str | None = None
    latest_release_commit_sha: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class ProjectAppAnalysisRunStart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    planner_version: str | None = None

    @field_validator("planner_version")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)


class ProjectAppAnalysisRunRead(BaseModel):
    run_id: str
    tenant_id: str
    project_id: str
    status: Literal["queued", "running", "completed", "failed"]
    planner_version: str | None = None
    request_payload: dict[str, object] = Field(default_factory=dict)
    result_payload: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class TenantDeploymentsOverviewSummaryRead(BaseModel):
    total_apps: int = 0
    draft_count: int = 0
    needs_pr_merge_count: int = 0
    ready_count: int = 0
    deploying_count: int = 0
    live_count: int = 0
    failed_count: int = 0


class TenantDeploymentsOverviewFailureRead(BaseModel):
    tenant_id: str
    tenant_name: str | None = None
    project_id: str
    project_name: str
    app_id: str
    app_name: str
    slug: str
    source_path: str | None = None
    status: str
    last_error: str | None = None
    last_release_id: str | None = None
    updated_at: datetime


class TenantDeploymentsOverviewAppRead(BaseModel):
    tenant_id: str
    tenant_name: str | None = None
    project_id: str
    project_name: str
    app_id: str
    app_name: str
    slug: str
    source_path: str | None = None
    status: str
    detection_confidence: float | None = None
    detected_runtime: str | None = None
    detected_language: str | None = None
    build_strategy: str | None = None
    last_release_status: str | None = None
    last_error: str | None = None
    updated_at: datetime


class TenantDeploymentsOverviewRead(BaseModel):
    summary: TenantDeploymentsOverviewSummaryRead = Field(default_factory=TenantDeploymentsOverviewSummaryRead)
    latest_failures: list[TenantDeploymentsOverviewFailureRead] = Field(default_factory=list)
    apps: list[TenantDeploymentsOverviewAppRead] = Field(default_factory=list)
    generated_at: datetime | None = None


class TenantDeploymentPlaneWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["internal_coolify"] = "internal_coolify"
    infrastructure_provider: Literal["aws", "hetzner"] | None = None
    region: str | None = None
    base_domain: str | None = None
    platform_subdomain: str | None = None
    api_base_url: str | None = None
    coolify_project_uuid: str | None = None
    coolify_environment_name: str | None = None
    coolify_server_uuid: str | None = None
    coolify_destination_uuid: str | None = None
    coolify_github_app_uuid: str | None = None
    managed_host_id: str | None = None
    secret_refs: dict[str, str] = Field(default_factory=dict)
    state: Literal["unconfigured", "provisioning", "active", "degraded", "paused", "failed"] = "unconfigured"
    last_error: str | None = None

    @field_validator(
        "region",
        "base_domain",
        "platform_subdomain",
        "api_base_url",
        "coolify_project_uuid",
        "coolify_environment_name",
        "coolify_server_uuid",
        "coolify_destination_uuid",
        "coolify_github_app_uuid",
        "managed_host_id",
        "last_error",
    )
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("secret_refs")
    @classmethod
    def normalize_secret_refs(cls, value: object) -> dict[str, str]:
        return _normalize_string_map(value)


class TenantDeploymentPlaneRead(TenantDeploymentPlaneWrite):
    pass


class DeploymentHostCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    provider: Literal["internal_coolify"] = "internal_coolify"
    infrastructure_provider: Literal["aws", "hetzner"] | None = None
    region: str | None = None
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: object) -> str:
        return _normalize_required_string(value)

    @field_validator("infrastructure_provider", "region")
    @classmethod
    def normalize_optional_host_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("capabilities")
    @classmethod
    def normalize_capabilities(cls, value: object) -> list[str]:
        normalized: list[str] = []
        if not isinstance(value, list):
            return normalized
        for item in value:
            capability = _normalize_lower_string(item)
            if capability and capability not in normalized:
                normalized.append(capability)
        return normalized


class DeploymentHostRead(BaseModel):
    host_id: str
    label: str
    provider: str
    infrastructure_provider: str | None = None
    region: str | None = None
    capabilities: list[str] = Field(default_factory=list)
    agent_version: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    state: Literal["provisioning", "active", "degraded", "offline", "retired"]
    registered_at: datetime | None = None
    last_seen_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DeploymentHostBootstrapRead(BaseModel):
    host: DeploymentHostRead
    bootstrap_token: str


class DeploymentHostRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bootstrap_token: str
    agent_version: str | None = None
    advertised_capabilities: list[str] = Field(default_factory=list)

    @field_validator("bootstrap_token", "agent_version")
    @classmethod
    def normalize_required_registration_strings(cls, value: object, info: ValidationInfo) -> str | None:
        normalized = _normalize_optional_string(value)
        if info.field_name == "bootstrap_token":
            if normalized is None:
                raise ValueError("bootstrap_token is required")
            return normalized
        return normalized

    @field_validator("advertised_capabilities")
    @classmethod
    def normalize_advertised_capabilities(cls, value: object) -> list[str]:
        return DeploymentHostCreate.normalize_capabilities(value)


class DeploymentHostRegistrationRead(BaseModel):
    host: DeploymentHostRead
    access_token: str


class DeploymentHostHeartbeatWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_version: str | None = None
    advertised_capabilities: list[str] = Field(default_factory=list)
    state: Literal["active", "degraded"] = "active"

    @field_validator("agent_version")
    @classmethod
    def normalize_agent_version(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("advertised_capabilities")
    @classmethod
    def normalize_advertised_capabilities(cls, value: object) -> list[str]:
        return DeploymentHostCreate.normalize_capabilities(value)


class DeploymentHostCommandRead(BaseModel):
    command_id: str
    host_id: str
    tenant_id: str | None = None
    project_id: str | None = None
    app_id: str | None = None
    restore_run_id: str | None = None
    kind: str
    status: Literal["queued", "claimed", "running", "succeeded", "failed", "expired", "canceled"]
    claim_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)
    result: dict[str, object] = Field(default_factory=dict)
    last_error: str | None = None
    available_at: datetime
    claimed_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class DeploymentHostCommandClaimRead(BaseModel):
    command: DeploymentHostCommandRead | None = None


class DeploymentHostCommandStartWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str

    @field_validator("claim_id")
    @classmethod
    def normalize_claim_id(cls, value: object) -> str:
        return _normalize_required_string(value)


class DeploymentHostCommandResultWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    status: Literal["succeeded", "failed"]
    result: dict[str, object] = Field(default_factory=dict)
    last_error: str | None = None

    @field_validator("claim_id", "last_error")
    @classmethod
    def normalize_command_result_strings(cls, value: object, info: ValidationInfo) -> str | None:
        normalized = _normalize_optional_string(value)
        if info.field_name == "claim_id":
            if normalized is None:
                raise ValueError("claim_id is required")
            return normalized
        return normalized


class ProjectDeploymentDomainWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    service_key: str
    host: str
    path: str | None = None
    tls_enabled: bool = True
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value: object) -> str:
        normalized = _normalize_required_string(value).lower()
        return normalized

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: object) -> str:
        return _normalize_lower_string(value)

    @field_validator("service_key")
    @classmethod
    def normalize_service_key(cls, value: object) -> str:
        return _normalize_lower_string(value)

    @field_validator("path")
    @classmethod
    def normalize_path(cls, value: object) -> str | None:
        normalized = _normalize_optional_string(value)
        if normalized is None:
            return None
        if not normalized.startswith("/"):
            raise ValueError("path must start with /")
        return normalized

    @model_serializer(mode="plain")
    def serialize_sparse(self) -> dict[str, object]:
        serialized = {
            "key": self.key,
            "service_key": self.service_key,
            "host": self.host,
        }
        if self.path is not None:
            serialized["path"] = self.path
        if self.tls_enabled is not True:
            serialized["tls_enabled"] = self.tls_enabled
        if self.config:
            serialized["config"] = self.config
        return serialized


class ProjectDeploymentResourceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    kind: str
    name: str | None = None
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: object) -> str:
        return _normalize_lower_string(value)

    @field_validator("kind")
    @classmethod
    def normalize_kind(cls, value: object) -> str:
        normalized = _normalize_lower_string(value)
        if normalized == "service":
            raise ValueError("Deployable services must be declared in services, not resources")
        if normalized in _DEPLOYMENT_VOLUME_KINDS:
            raise ValueError("Persistent volumes must be declared in volumes, not resources")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("config")
    @classmethod
    def validate_config_has_no_raw_secrets(cls, value: object) -> dict[str, object]:
        config = dict(value) if isinstance(value, dict) else {}
        _assert_no_raw_secret_config(config)
        return config

    @model_serializer(mode="plain")
    def serialize_sparse(self) -> dict[str, object]:
        serialized = {
            "key": self.key,
            "kind": self.kind,
        }
        if self.name is not None:
            serialized["name"] = self.name
        if self.config:
            serialized["config"] = self.config
        return serialized


class ProjectDeploymentServiceWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    name: str | None = None
    kind: Literal["api", "website"]
    source_path: str | None = None
    compose_service: str | None = None
    build_strategy: str | None = None
    container_port: int | None = Field(default=None, ge=1, le=65535)
    public: bool = True
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: object) -> str:
        return _normalize_lower_string(value)

    @field_validator("name", "source_path", "compose_service", "build_strategy")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("config")
    @classmethod
    def validate_config_has_no_raw_secrets(cls, value: object) -> dict[str, object]:
        config = dict(value) if isinstance(value, dict) else {}
        _assert_no_raw_secret_config(config)
        return config

    @model_serializer(mode="plain")
    def serialize_sparse(self) -> dict[str, object]:
        serialized: dict[str, object] = {
            "key": self.key,
            "kind": self.kind,
            "public": self.public,
        }
        if self.name is not None:
            serialized["name"] = self.name
        if self.source_path is not None:
            serialized["source_path"] = self.source_path
        if self.compose_service is not None:
            serialized["compose_service"] = self.compose_service
        if self.build_strategy is not None:
            serialized["build_strategy"] = self.build_strategy
        if self.container_port is not None:
            serialized["container_port"] = self.container_port
        if self.config:
            serialized["config"] = self.config
        return serialized


class ProjectDeploymentVolumeWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    type: Literal["persistent", "file"] = "persistent"
    name: str | None = None
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: object) -> str:
        return _normalize_lower_string(value)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("config")
    @classmethod
    def validate_config_has_no_raw_secrets(cls, value: object) -> dict[str, object]:
        config = dict(value) if isinstance(value, dict) else {}
        _assert_no_raw_secret_config(config)
        return config

    @model_serializer(mode="plain")
    def serialize_sparse(self) -> dict[str, object]:
        serialized: dict[str, object] = {
            "key": self.key,
            "type": self.type,
        }
        if self.name is not None:
            serialized["name"] = self.name
        if self.config:
            serialized["config"] = self.config
        return serialized


class ProjectDeploymentBackupPolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    resource_key: str
    enabled: bool = True
    schedule: str | None = None
    retention_days: int | None = Field(default=None, ge=1)
    config: dict[str, object] = Field(default_factory=dict)

    @field_validator("key", "resource_key")
    @classmethod
    def normalize_keys(cls, value: object) -> str:
        return _normalize_lower_string(value)

    @field_validator("schedule")
    @classmethod
    def normalize_schedule(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("config")
    @classmethod
    def validate_config_has_no_raw_secrets(cls, value: object) -> dict[str, object]:
        config = dict(value) if isinstance(value, dict) else {}
        _assert_no_raw_secret_config(config)
        return config

    @model_serializer(mode="plain")
    def serialize_sparse(self) -> dict[str, object]:
        serialized = {
            "key": self.key,
            "resource_key": self.resource_key,
        }
        if self.enabled is not True:
            serialized["enabled"] = self.enabled
        if self.schedule is not None:
            serialized["schedule"] = self.schedule
        if self.retention_days is not None:
            serialized["retention_days"] = self.retention_days
        if self.config:
            serialized["config"] = self.config
        return serialized


class ProjectDeploymentConfigWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Literal[True] = True
    environment_name: str | None = None
    source_strategy: Literal["dockerfile", "docker_compose"] | None = None
    source_branch: str | None = None
    source_commit_sha: str | None = None
    deployment_branch: str | None = None
    deployment_commit_sha: str | None = None
    deployment_compose_path: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    domains: list[ProjectDeploymentDomainWrite] = Field(default_factory=list)
    services: list[ProjectDeploymentServiceWrite] = Field(default_factory=list)
    resources: list[ProjectDeploymentResourceWrite] = Field(default_factory=list)
    volumes: list[ProjectDeploymentVolumeWrite] = Field(default_factory=list)
    backup_policies: list[ProjectDeploymentBackupPolicyWrite] = Field(default_factory=list)
    generated_compose_raw: str | None = Field(default=None, exclude=True)
    deployment_plan: dict[str, object] = Field(default_factory=dict, exclude=True)

    @field_validator("environment_name")
    @classmethod
    def normalize_environment_name(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("source_strategy")
    @classmethod
    def normalize_source_strategy(cls, value: object) -> str | None:
        normalized = _normalize_optional_string(value)
        if normalized is None:
            return None
        lowered = normalized.lower()
        if lowered not in {"dockerfile", "docker_compose"}:
            raise ValueError("source_strategy must be dockerfile or docker_compose")
        return lowered

    @field_validator("source_branch", "deployment_branch", "deployment_compose_path")
    @classmethod
    def normalize_optional_deployment_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("source_commit_sha", "deployment_commit_sha")
    @classmethod
    def normalize_optional_commit_sha(cls, value: object) -> str | None:
        normalized = _normalize_optional_string(value)
        if normalized is None:
            return None
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", normalized):
            raise ValueError("commit sha must be a 7-64 character hexadecimal git commit")
        return normalized

    @field_validator("environment")
    @classmethod
    def normalize_environment(cls, value: object) -> dict[str, str]:
        normalized = _normalize_string_map(value)
        _assert_no_raw_secret_config(normalized, path="environment")
        return normalized

    @field_validator("secret_refs")
    @classmethod
    def normalize_secret_refs(cls, value: object) -> dict[str, str]:
        return _normalize_string_map(value)

    @model_validator(mode="after")
    def validate_relationships(self) -> "ProjectDeploymentConfigWrite":
        if self.source_strategy == "docker_compose":
            if not self.deployment_branch:
                raise ValueError("docker_compose deployment config requires deployment_branch")
            if not self.deployment_commit_sha:
                raise ValueError("docker_compose deployment config requires deployment_commit_sha")
            if not self.deployment_compose_path:
                raise ValueError("docker_compose deployment config requires deployment_compose_path")

        domain_keys: set[str] = set()
        hosts: set[str] = set()
        for domain in self.domains:
            if domain.key in domain_keys:
                raise ValueError(f"Duplicate deployment domain key: {domain.key}")
            domain_keys.add(domain.key)
            normalized_host = domain.host.lower()
            if normalized_host in hosts:
                raise ValueError(f"Duplicate deployment domain host: {domain.host}")
            hosts.add(normalized_host)

        resource_keys: set[str] = set()
        for resource in self.resources:
            if resource.key in resource_keys:
                raise ValueError(f"Duplicate deployment resource key: {resource.key}")
            resource_keys.add(resource.key)

        service_keys: set[str] = set()
        for service in self.services:
            if service.key in service_keys:
                raise ValueError(f"Duplicate deployment service key: {service.key}")
            if service.key in resource_keys:
                raise ValueError(f"Deployment service key duplicates resource key: {service.key}")
            service_keys.add(service.key)

        volume_keys: set[str] = set()
        for volume in self.volumes:
            if volume.key in volume_keys:
                raise ValueError(f"Duplicate deployment volume key: {volume.key}")
            if volume.key in resource_keys:
                raise ValueError(f"Deployment volume key duplicates resource key: {volume.key}")
            if volume.key in service_keys:
                raise ValueError(f"Deployment volume key duplicates service key: {volume.key}")
            volume_keys.add(volume.key)

        backup_keys: set[str] = set()
        for backup_policy in self.backup_policies:
            if backup_policy.key in backup_keys:
                raise ValueError(f"Duplicate deployment backup policy key: {backup_policy.key}")
            backup_keys.add(backup_policy.key)
            if backup_policy.resource_key not in resource_keys:
                raise ValueError(
                    f"Backup policy '{backup_policy.key}' references unknown resource '{backup_policy.resource_key}'"
                )
        return self


def _normalize_production_branch(value: object) -> str | None:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        return None
    for prefix in ("refs/heads/", "origin/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):].strip()
            break
    if not normalized:
        raise ValueError("production_branch is required")
    return normalized


class ProjectDeploymentPolicyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    production_branch: str | None = None
    preview_prs_enabled: bool = False
    provider: Literal["internal_coolify"] = "internal_coolify"
    deployment_host_id: str | None = None
    generated_domain_policy: Literal["disabled", "production", "production_and_preview"] = "production"
    branch_settings: dict[str, "ProjectDeploymentBranchSettings"] = Field(default_factory=dict)
    services: list[ProjectDeploymentServiceWrite] = Field(default_factory=list)
    resources: list[ProjectDeploymentResourceWrite] = Field(default_factory=list)
    volumes: list[ProjectDeploymentVolumeWrite] = Field(default_factory=list)

    @field_validator("production_branch")
    @classmethod
    def normalize_production_branch(cls, value: object) -> str | None:
        return _normalize_production_branch(value)

    @field_validator("deployment_host_id")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("branch_settings")
    @classmethod
    def normalize_branch_settings(cls, value: object) -> dict[str, "ProjectDeploymentBranchSettings"]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError("branch_settings must be an object")
        normalized: dict[str, ProjectDeploymentBranchSettings] = {}
        for raw_branch, raw_settings in value.items():
            branch = _normalize_production_branch(raw_branch)
            if branch is None:
                raise ValueError("branch_settings keys must be branch names")
            if branch in normalized:
                raise ValueError(f"Duplicate deployment branch settings for {branch}")
            normalized[branch] = ProjectDeploymentBranchSettings.model_validate(raw_settings)
        return normalized

    @model_validator(mode="after")
    def validate_policy(self) -> "ProjectDeploymentPolicyWrite":
        if self.enabled and not self.production_branch:
            raise ValueError("production_branch is required when deployments are enabled")
        if self.preview_prs_enabled:
            raise ValueError("preview PR deployments are not supported by the GitHub deployment worker yet")
        resource_keys: set[str] = set()
        for resource in self.resources:
            if resource.key in resource_keys:
                raise ValueError(f"Duplicate deployment resource key: {resource.key}")
            resource_keys.add(resource.key)
        service_keys: set[str] = set()
        for service in self.services:
            if service.key in service_keys:
                raise ValueError(f"Duplicate deployment service key: {service.key}")
            if service.key in resource_keys:
                raise ValueError(f"Deployment service key duplicates resource key: {service.key}")
            service_keys.add(service.key)
        volume_keys: set[str] = set()
        for volume in self.volumes:
            if volume.key in volume_keys:
                raise ValueError(f"Duplicate deployment volume key: {volume.key}")
            if volume.key in resource_keys:
                raise ValueError(f"Deployment volume key duplicates resource key: {volume.key}")
            if volume.key in service_keys:
                raise ValueError(f"Deployment volume key duplicates service key: {volume.key}")
            volume_keys.add(volume.key)
        return self


class ProjectDeploymentPolicyRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    production_branch: str | None = None
    preview_prs_enabled: bool = False
    provider: Literal["internal_coolify"] = "internal_coolify"
    deployment_host_id: str | None = None
    generated_domain_policy: Literal["disabled", "production", "production_and_preview"] = "production"
    branch_settings: dict[str, "ProjectDeploymentBranchSettings"] = Field(default_factory=dict)
    services: list[ProjectDeploymentServiceWrite] = Field(default_factory=list)
    resources: list[ProjectDeploymentResourceWrite] = Field(default_factory=list)
    volumes: list[ProjectDeploymentVolumeWrite] = Field(default_factory=list)

    @field_validator("production_branch")
    @classmethod
    def normalize_production_branch(cls, value: object) -> str | None:
        return _normalize_production_branch(value)

    @field_validator("deployment_host_id")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)

    @field_validator("branch_settings")
    @classmethod
    def normalize_branch_settings(cls, value: object) -> dict[str, "ProjectDeploymentBranchSettings"]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError("branch_settings must be an object")
        normalized: dict[str, ProjectDeploymentBranchSettings] = {}
        for raw_branch, raw_settings in value.items():
            branch = _normalize_production_branch(raw_branch)
            if branch is None:
                raise ValueError("branch_settings keys must be branch names")
            if branch in normalized:
                raise ValueError(f"Duplicate deployment branch settings for {branch}")
            normalized[branch] = ProjectDeploymentBranchSettings.model_validate(raw_settings)
        return normalized

    @model_validator(mode="after")
    def validate_policy(self) -> "ProjectDeploymentPolicyRead":
        if self.enabled and not self.production_branch:
            raise ValueError("production_branch is required when deployments are enabled")
        if self.preview_prs_enabled:
            raise ValueError("preview PR deployments are not supported by the GitHub deployment worker yet")
        resource_keys: set[str] = set()
        for resource in self.resources:
            if resource.key in resource_keys:
                raise ValueError(f"Duplicate deployment resource key: {resource.key}")
            resource_keys.add(resource.key)
        service_keys: set[str] = set()
        for service in self.services:
            if service.key in service_keys:
                raise ValueError(f"Duplicate deployment service key: {service.key}")
            if service.key in resource_keys:
                raise ValueError(f"Deployment service key duplicates resource key: {service.key}")
            service_keys.add(service.key)
        volume_keys: set[str] = set()
        for volume in self.volumes:
            if volume.key in volume_keys:
                raise ValueError(f"Duplicate deployment volume key: {volume.key}")
            if volume.key in resource_keys:
                raise ValueError(f"Deployment volume key duplicates resource key: {volume.key}")
            if volume.key in service_keys:
                raise ValueError(f"Deployment volume key duplicates service key: {volume.key}")
            volume_keys.add(volume.key)
        return self


class ProjectDeploymentSetupStartRead(BaseModel):
    workflow_id: str
    status: Literal["started"]
    policy: ProjectDeploymentPolicyRead


class ProjectDeploymentBranchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)

    @field_validator("environment", "secret_refs")
    @classmethod
    def normalize_string_maps(cls, value: object) -> dict[str, str]:
        return _normalize_string_map(value)


class ProjectDeploymentConfigRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: Literal[True] = True
    environment_name: str | None = None
    source_strategy: Literal["dockerfile", "docker_compose"] | None = None
    source_branch: str | None = None
    source_commit_sha: str | None = None
    deployment_branch: str | None = None
    deployment_commit_sha: str | None = None
    deployment_compose_path: str | None = None
    environment: dict[str, str] = Field(default_factory=dict)
    secret_refs: dict[str, str] = Field(default_factory=dict)
    domains: list[ProjectDeploymentDomainWrite] = Field(default_factory=list)
    services: list[ProjectDeploymentServiceWrite] = Field(default_factory=list)
    resources: list[ProjectDeploymentResourceWrite] = Field(default_factory=list)
    volumes: list[ProjectDeploymentVolumeWrite] = Field(default_factory=list)
    backup_policies: list[ProjectDeploymentBackupPolicyWrite] = Field(default_factory=list)
    generated_compose_raw: str | None = Field(default=None, exclude=True)
    deployment_plan: dict[str, object] = Field(default_factory=dict, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def redact_raw_secret_config(cls, value: object) -> dict[str, object]:
        redacted = redact_deployment_config_secrets(value)
        redacted["enabled"] = True
        return redacted

    @model_validator(mode="after")
    def validate_relationships(self) -> "ProjectDeploymentConfigRead":
        return self


class ProjectDeploymentReleaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_id: str | None = None
    git_ref: str
    commit_sha: str
    reason: str | None = None

    @field_validator("git_ref", "commit_sha")
    @classmethod
    def normalize_required_git_source(cls, value: object, info: ValidationInfo) -> str:
        normalized = _normalize_required_string(value)
        if info.field_name == "commit_sha" and not re.fullmatch(r"[0-9a-fA-F]{7,64}", normalized):
            raise ValueError("commit_sha must be a Git commit SHA")
        return normalized

    @field_validator("app_id", "reason")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)


class ProjectDeploymentServiceUrlRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_key: str
    service_name: str
    service_kind: Literal["website", "api"]
    url: str
    url_kind: Literal["generated", "custom"]
    internal_url: str | None = None
    status: Literal["pending", "active", "failed"] = "active"
    port: int | None = None
    proxy_port: int | None = None
    host: str | None = None
    domain_key: str | None = None
    release_id: str | None = None

    @field_validator("service_key", "service_name", "url", "internal_url", "host", "domain_key", "release_id")
    @classmethod
    def normalize_optional_strings(cls, value: object, info: ValidationInfo) -> str | None:
        normalized = _normalize_optional_string(value)
        if info.field_name in {"service_key", "service_name", "url"}:
            if normalized is None:
                raise ValueError(f"{info.field_name} is required")
            return normalized
        return normalized


class ProjectDeploymentReleaseRead(BaseModel):
    release_id: str
    tenant_id: str
    project_id: str
    app_id: str | None = None
    provider: str
    status: str
    environment_name: str | None = None
    source_strategy: str | None = None
    git_ref: str
    commit_sha: str
    release_name: str
    requested_by_user_id: str | None = None
    deployment_snapshot: dict[str, object] = Field(default_factory=dict)
    provider_context: dict[str, object] = Field(default_factory=dict)
    service_urls: list[ProjectDeploymentServiceUrlRead] = Field(default_factory=list)
    last_error: str | None = None
    requested_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ProjectDeploymentReleaseStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["queued", "provisioning", "deploying", "route_activating", "live", "failed", "rolled_back"]
    last_error: str | None = None
    deployment_uuid: str | None = None

    @field_validator("last_error", "deployment_uuid")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)


class ProjectDeploymentResourceApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_keys: list[str] = Field(default_factory=list)

    @field_validator("resource_keys")
    @classmethod
    def normalize_resource_keys(cls, value: object) -> list[str]:
        return _normalize_unique_lower_key_list(value)


class ProjectDeploymentVolumeApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_keys: list[str] = Field(default_factory=list)

    @field_validator("volume_keys")
    @classmethod
    def normalize_volume_keys(cls, value: object) -> list[str]:
        return _normalize_unique_lower_key_list(value)


class ProjectDeploymentDomainApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    domain_keys: list[str] = Field(default_factory=list)

    @field_validator("domain_keys")
    @classmethod
    def normalize_domain_keys(cls, value: object) -> list[str]:
        return _normalize_unique_lower_key_list(value)


class ProjectDeploymentBackupApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_keys: list[str] = Field(default_factory=list)

    @field_validator("backup_keys")
    @classmethod
    def normalize_backup_keys(cls, value: object) -> list[str]:
        return _normalize_unique_lower_key_list(value)


class ProjectDeploymentBackupTriggerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_keys: list[str] = Field(default_factory=list)

    @field_validator("backup_keys")
    @classmethod
    def normalize_backup_keys(cls, value: object) -> list[str]:
        return _normalize_unique_lower_key_list(value)


class ProjectDeploymentBackupRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_key: str
    resource_key: str
    execution_uuid: str
    confirmation_value: str
    backup_uuid: str | None = None

    @field_validator("backup_key", "resource_key", "execution_uuid", "confirmation_value", "backup_uuid")
    @classmethod
    def normalize_optional_strings(cls, value: object) -> str | None:
        return _normalize_optional_string(value)


class ProjectDeploymentBackupExecutionRead(BaseModel):
    execution_uuid: str
    status: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    artifact_path: str | None = None
    file_name: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class ProjectDeploymentBackupExecutionListRead(BaseModel):
    backup_key: str
    resource_key: str
    backup_uuid: str | None = None
    database_uuid: str | None = None
    executions: list[ProjectDeploymentBackupExecutionRead] = Field(default_factory=list)


class ProjectDeploymentRestoreRunRead(BaseModel):
    restore_run_id: str
    tenant_id: str
    project_id: str
    app_id: str
    host_id: str | None = None
    command_id: str | None = None
    backup_policy_key: str
    resource_key: str
    backup_uuid: str | None = None
    execution_uuid: str
    database_type: Literal["postgres", "mysql", "mariadb"]
    database_uuid: str
    restore_mode: Literal["replace"]
    requested_by_user_id: str | None = None
    confirmation_value: str
    execution_payload: dict[str, object] = Field(default_factory=dict)
    status: Literal["queued", "running", "succeeded", "failed"]
    last_error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime


class ProjectDeploymentOperationItemRead(BaseModel):
    key: str
    kind: str | None = None
    status: Literal["applied", "skipped", "unsupported", "failed"]
    provider_uuid: str | None = None
    message: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class ProjectDeploymentOperationRead(BaseModel):
    operation: Literal[
        "apply_resources",
        "apply_volumes",
        "apply_domains",
        "apply_backups",
        "trigger_backups",
        "restore_backups",
    ]
    tenant_id: str
    project_id: str
    provider: str
    application_uuid: str | None = None
    items: list[ProjectDeploymentOperationItemRead] = Field(default_factory=list)
    applied_count: int = 0
    skipped_count: int = 0
    unsupported_count: int = 0
    failed_count: int = 0
    executed_at: datetime
