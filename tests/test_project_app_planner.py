from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import yaml

from orchestrator.core.project_app_analysis_runtime import run_project_app_analysis
from orchestrator.core.deployment_setup.planner import DeploymentPlannerResponse, run_project_deployment_planning
from orchestrator.core.project_app_planner import (
    ProjectAppNormalizedCandidate,
    ProjectAppPreScanCandidate,
    persist_project_app_analysis_result,
    normalize_project_app_planner_output,
    scan_repo_for_project_apps,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, ProjectApp, ProjectAppAnalysisRun, Tenant


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _dockerfile_pre_scan_candidate(source_path: str) -> ProjectAppPreScanCandidate:
    return ProjectAppPreScanCandidate(
        name=Path(source_path).name,
        source_path=source_path,
        build_strategy="dockerfile",
        detected_runtime="python",
        detected_language="python",
        detection_confidence=0.9,
        exposed_port=8080,
        healthcheck=None,
        start_command=None,
        env_schema_json={},
        secret_schema_json={},
        deployment_config={},
        analysis_source="deployment_setup",
        needs_generated_files=False,
    )


def test_scan_repo_for_project_apps_detects_dockerfile_and_framework_candidates() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "Dockerfile",
            """\
FROM python:3.11-slim
EXPOSE 8000
CMD ["python", "app.py"]
""",
        )
        _write(
            root / "frontend" / "package.json",
            """\
{
  "name": "frontend-app",
  "dependencies": {
    "next": "^14.0.0"
  }
}
""",
        )

        candidates = scan_repo_for_project_apps(checkout_path=str(root), analysis_source="manual_analyze")

    assert {candidate.source_path for candidate in candidates} == {".", "frontend"}
    root_candidate = next(candidate for candidate in candidates if candidate.source_path == ".")
    frontend_candidate = next(candidate for candidate in candidates if candidate.source_path == "frontend")
    assert root_candidate.build_strategy == "dockerfile"
    assert root_candidate.exposed_port == 8000
    assert root_candidate.needs_generated_files is False
    assert frontend_candidate.build_strategy == "nixpacks"
    assert frontend_candidate.detected_runtime == "nextjs"
    assert frontend_candidate.exposed_port == 3000
    assert frontend_candidate.needs_generated_files is True


def test_scan_repo_for_project_apps_extracts_docker_compose_resources() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "docker-compose.yml",
            """\
services:
  web:
    image: example/web:latest
    ports:
      - "3000:3000"
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: bsktpay
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
    volumes:
      - postgres-data:/var/lib/postgresql/data
  redis:
    image: redis:7
  mailpit:
    image: axllent/mailpit:v1.28.2
    environment:
      MP_SMTP_BIND_ADDR: 0.0.0.0:1025
    ports:
      - "1025:1025"
  activemq:
    image: apache/activemq-classic:6.1.7
    ports:
      - "61616:61616"
  kafka:
    image: apache/kafka:4.2.0
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.10
    volumes:
      - elasticsearch-data:/usr/share/elasticsearch/data
  dejavu:
    image: appbaseio/dejavu
    ports:
      - "1358:1358"
  temporal-ui:
    image: temporalio/ui:2.31.2
    ports:
      - "8088:8088"
  temporal:
    image: temporalio/auto-setup:1.25.2
    ports:
      - "7233:7233"
volumes:
  postgres-data: {}
  elasticsearch-data: {}
""",
        )

        candidates = scan_repo_for_project_apps(checkout_path=str(root), analysis_source="manual_analyze")

    candidate = candidates[0]
    assert candidate.source_path == "."
    assert candidate.build_strategy == "docker_compose"
    assert candidate.exposed_port == 3000
    assert candidate.services_json == (
        {
            "key": "web",
            "kind": "website",
            "name": "web",
            "compose_service": "web",
            "public": True,
            "config": {
                "source": "docker_compose",
                "image": "example/web:latest",
                "ports": [{"host_port": 3000, "container_port": 3000, "protocol": "tcp"}],
            },
        },
    )
    assert candidate.resources_json == (
        {
            "key": "activemq",
            "kind": "activemq",
            "name": "activemq",
            "config": {
                "compose_service": "activemq",
                "source": "docker_compose",
                "image": "apache/activemq-classic:6.1.7",
                "service_type": "activemq",
                "ports": [{"host_port": 61616, "container_port": 61616, "protocol": "tcp"}],
            },
        },
        {
            "key": "elasticsearch",
            "kind": "elasticsearch",
            "name": "elasticsearch",
            "config": {
                "compose_service": "elasticsearch",
                "source": "docker_compose",
                "image": "docker.elastic.co/elasticsearch/elasticsearch:8.19.10",
                "service_type": "elasticsearch",
            },
        },
        {
            "key": "kafka",
            "kind": "kafka",
            "name": "kafka",
            "config": {
                "compose_service": "kafka",
                "source": "docker_compose",
                "image": "apache/kafka:4.2.0",
                "service_type": "kafka",
            },
        },
        {
            "key": "mailpit",
            "kind": "smtp",
            "name": "mailpit",
            "config": {
                "compose_service": "mailpit",
                "source": "docker_compose",
                "image": "axllent/mailpit:v1.28.2",
                "service_type": "mailpit",
                "ports": [{"host_port": 1025, "container_port": 1025, "protocol": "tcp"}],
            },
        },
        {
            "key": "postgres",
            "kind": "postgres",
            "name": "postgres",
            "config": {
                "compose_service": "postgres",
                "source": "docker_compose",
                "image": "postgres:16",
            },
        },
        {
            "key": "redis",
            "kind": "redis",
            "name": "redis",
            "config": {
                "compose_service": "redis",
                "source": "docker_compose",
                "image": "redis:7",
            },
        },
    )
    assert candidate.volumes_json == (
        {
            "key": "elasticsearch-data",
            "type": "persistent",
            "name": "elasticsearch-data",
            "config": {
                "compose_volume": "elasticsearch-data",
                "source": "docker_compose",
                "mount_path": "/usr/share/elasticsearch/data",
            },
        },
        {
            "key": "postgres-data",
            "type": "persistent",
            "name": "postgres-data",
            "config": {
                "compose_volume": "postgres-data",
                "source": "docker_compose",
                "mount_path": "/var/lib/postgresql/data",
            },
        },
    )
    assert candidate.deployment_config["source_strategy"] == "docker_compose"
    assert candidate.deployment_config["services"] == [dict(service) for service in candidate.services_json]
    assert candidate.deployment_config["resources"] == [dict(resource) for resource in candidate.resources_json]
    assert candidate.deployment_config["volumes"] == [dict(volume) for volume in candidate.volumes_json]


def test_scan_repo_for_project_apps_detects_maven_services_without_treating_java_packages_as_apps() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "api" / "identity" / "pom.xml",
            """\
<project>
  <dependencies>
    <dependency>
      <groupId>org.springframework.boot</groupId>
      <artifactId>spring-boot-starter-web</artifactId>
    </dependency>
  </dependencies>
</project>
""",
        )
        _write(root / "api" / "identity" / "src" / "main" / "java" / "com" / "example" / "User.java", "class User {}")

        candidates = scan_repo_for_project_apps(checkout_path=str(root), analysis_source="manual_analyze")

    assert [candidate.source_path for candidate in candidates] == ["api/identity"]
    assert candidates[0].detected_runtime == "spring_boot"
    assert candidates[0].detected_language == "java"
    assert candidates[0].exposed_port == 8080


def test_scan_repo_for_project_apps_ignores_non_deployable_metadata_directories() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "docs" / ".env.example",
            "API_KEY=\n",
        )
        _write(
            root / "packages" / "models" / "package.json",
            """\
{
  "name": "models"
}
""",
        )

        candidates = scan_repo_for_project_apps(checkout_path=str(root), analysis_source="manual_analyze")

    assert len(candidates) == 1
    assert candidates[0].source_path == "."
    assert candidates[0].detected_runtime is None
    assert candidates[0].needs_generated_files is True


def test_normalize_project_app_planner_output_merges_runtime_payload_and_assigns_unique_slugs() -> None:
    pre_scan_candidates = (
        SimpleNamespace(
            name="api-service",
            source_path=".",
            build_strategy="dockerfile",
            detected_runtime="python",
            detected_language="python",
            detection_confidence=0.9,
            exposed_port=8000,
            healthcheck="CMD curl -f http://localhost:8000/health || exit 1",
            start_command="python app.py",
            env_schema_json={"APP_ENV": {"required": True}},
            secret_schema_json={},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=False,
            resources_json=(),
            env_json={},
            secret_json={},
        ),
        SimpleNamespace(
            name="frontend-app",
            source_path="frontend",
            build_strategy="nixpacks",
            detected_runtime="nextjs",
            detected_language="javascript",
            detection_confidence=0.85,
            exposed_port=3000,
            healthcheck=None,
            start_command="npm run start",
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=True,
            resources_json=(),
            env_json={},
            secret_json={},
        ),
    )
    runtime_payload = {
        "apps": [
            {
                "name": "api service",
                "source_path": ".",
                "build_strategy": "dockerfile",
                "port": 9000,
                "healthcheck": "/healthz",
                "resources": [{"kind": "postgres", "name": "db"}],
                "env": {"DATABASE_URL": {"required": True}},
                "secrets": {"API_KEY": {"required": True}},
                "needs_generated_files": False,
            },
            {
                "name": "frontend-app",
                "source_path": "frontend",
                "build_strategy": "nixpacks",
                "port": 4173,
                "healthcheck": None,
                "resources": [],
                "env": {},
                "secrets": {},
                "needs_generated_files": True,
            },
        ]
    }

    normalized = normalize_project_app_planner_output(
        pre_scan_candidates=pre_scan_candidates,
        runtime_payload=runtime_payload,
        analysis_source="manual_analyze",
    )

    assert [candidate.source_path for candidate in normalized] == [".", "frontend"]
    assert len({candidate.slug for candidate in normalized}) == 2
    api_candidate = normalized[0]
    frontend_candidate = normalized[1]
    assert api_candidate.name == "api service"
    assert api_candidate.build_strategy == "dockerfile"
    assert api_candidate.exposed_port == 9000
    assert api_candidate.status == "ready"
    assert api_candidate.needs_generated_files is False
    assert api_candidate.env_schema_json["DATABASE_URL"]["required"] is True
    assert frontend_candidate.status == "needs_pr_merge"
    assert frontend_candidate.needs_generated_files is True


def test_normalize_project_app_planner_output_preserves_prescan_compose_resources() -> None:
    pre_scan_candidates = (
        SimpleNamespace(
            name="compose-app",
            source_path=".",
            build_strategy="docker_compose",
            detected_runtime="docker",
            detected_language=None,
            detection_confidence=0.95,
            exposed_port=3000,
            healthcheck=None,
            start_command=None,
            env_schema_json={},
            secret_schema_json={},
            deployment_config={
                "source_strategy": "docker_compose",
                "resources": [
                    {
                        "key": "postgres",
                        "kind": "postgres",
                        "name": "postgres",
                        "config": {"compose_service": "postgres", "source": "docker_compose"},
                    }
                ],
                "backup_policies": [],
            },
            analysis_source="manual_analyze",
            needs_generated_files=False,
            resources_json=(
                {
                    "key": "postgres",
                    "kind": "postgres",
                    "name": "postgres",
                    "config": {"compose_service": "postgres", "source": "docker_compose"},
                },
            ),
            env_json={},
            secret_json={},
        ),
    )

    normalized = normalize_project_app_planner_output(
        pre_scan_candidates=pre_scan_candidates,
        runtime_payload={
            "apps": [
                {
                    "name": "compose-app",
                    "source_path": ".",
                    "build_strategy": "docker_compose",
                    "port": 3000,
                    "healthcheck": None,
                    "resources": [],
                    "env": {},
                    "secrets": {},
                    "needs_generated_files": False,
                }
            ]
        },
        analysis_source="manual_analyze",
    )

    assert normalized[0].resources_json == pre_scan_candidates[0].resources_json
    assert normalized[0].deployment_config["resources"] == pre_scan_candidates[0].deployment_config["resources"]


def test_run_project_app_analysis_returns_normalized_apps_and_metadata() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "Dockerfile", "FROM node:20\nEXPOSE 3000\n")
        payload = {
            "apps": [
                {
                    "name": "app",
                    "source_path": ".",
                    "build_strategy": "dockerfile",
                    "port": 3000,
                    "healthcheck": None,
                    "resources": [],
                    "env": {},
                    "secrets": {},
                    "needs_generated_files": False,
                }
            ]
        }
        with (
            patch(
                "orchestrator.core.project_app_analysis_runtime.build_codex_runtime",
                return_value=SimpleNamespace(),
            ),
            patch(
                "orchestrator.core.project_app_analysis_runtime.invoke_runtime_json",
                return_value=payload,
            ),
        ):
            result = run_project_app_analysis(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(project_id="project-1", name="Project 1", github_repository="https://github.com/example/repo"),
                checkout_path=str(root),
                analysis_source="manual_analyze",
                planner_version="planner-v1",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    assert result.metadata.tenant_id == "tenant-1"
    assert result.metadata.project_id == "project-1"
    assert result.metadata.analysis_source == "manual_analyze"
    assert result.metadata.pre_scan_count == 1
    assert result.metadata.runtime_count == 1
    assert result.metadata.normalized_count == 1
    assert result.apps[0].source_path == "."
    assert result.apps[0].status == "ready"


def test_run_project_app_analysis_sends_compose_evidence_to_model_runtime() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "docker-compose.yml",
            """\
services:
  temporal-ui:
    image: temporalio/ui:2.31.2
    ports:
      - "8088:8088"
""",
        )
        _write(root / "api" / "Dockerfile", "FROM python:3.11\nEXPOSE 8000\n")
        with (
            patch(
                "orchestrator.core.project_app_analysis_runtime.build_codex_runtime",
                return_value=SimpleNamespace(),
            ),
            patch(
                "orchestrator.core.project_app_analysis_runtime.invoke_runtime_json",
                return_value={
                    "apps": [
                        {
                            "name": "api",
                            "source_path": "api",
                            "build_strategy": "dockerfile",
                            "port": 8000,
                            "healthcheck": None,
                            "resources": [],
                            "env": {},
                            "secrets": {},
                            "needs_generated_files": False,
                        }
                    ]
                },
            ) as invoke,
        ):
            result = run_project_app_analysis(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Project 1",
                    github_repository="https://github.com/example/repo",
                ),
                checkout_path=str(root),
                analysis_source="manual_analyze",
                planner_version="planner-v1",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    assert result.metadata.pre_scan_count == 2
    assert result.metadata.runtime_count == 1
    assert result.metadata.normalized_count == 2
    assert result.metadata.raw_planner_result_json["apps"][0]["source_path"] == "api"
    assert invoke.call_count == 1
    prompt_payload = invoke.call_args.kwargs["user_prompt"]
    assert "docker_compose" in prompt_payload
    assert "temporal-ui" not in prompt_payload
    api_app = next(app for app in result.apps if app.source_path == "api")
    assert api_app.build_strategy == "dockerfile"
    assert api_app.status == "ready"


def test_run_project_deployment_planning_builds_one_repo_level_deployment_from_agent_plan() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        for module in ("identity-api", "payment-api", "search-api"):
            _write(root / "api" / module / "pom.xml", "<project><artifactId>spring-boot-starter-web</artifactId></project>")
            _write(root / "api" / module / "src" / "main" / "resources" / "application.properties", "spring.application.name=%s\n" % module)
        _write(root / "api" / "shared-library" / "pom.xml", "<project><artifactId>spring-boot-starter-web</artifactId></project>")
        for module in ("admin-ui", "customer-ui"):
            _write(
                root / "web" / module / "package.json",
                """\
{"name": "%s", "dependencies": {"vite": "^5.0.0"}}
"""
                % module,
            )
        _write(root / "web" / "legacy-ui" / "package.json", '{"name":"legacy-ui","scripts":{"start":"vite --host 0.0.0.0"}}')
        _write(
            root / "machine-learning" / "analytics" / "Dockerfile",
            "FROM europe-west2-docker.pkg.dev/example/private/runtime\nCMD [\"python\", \"app.py\"]\n",
        )
        _write(
            root / ".master-builder" / "deployments" / "docker-compose.yml",
            """\
services:
  stale-generated-service:
    image: nginx:alpine
""",
        )
        _write(
            root / "infra" / "docker-compose.yml",
            """\
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: app
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.10
  activemq:
    image: apache/activemq-classic:6.1.7
  temporal:
    image: temporalio/auto-setup:1.25.2
""",
        )
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "identity-api",
                        "name": "Identity API",
                        "kind": "api",
                        "source_path": "api/identity-api",
                        "build_strategy": "maven",
                        "compose_service": "identity-api",
                        "container_port": 8080,
                        "depends_on": ["postgres", "activemq"],
                    },
                    {
                        "key": "payment-api",
                        "name": "Payment API",
                        "kind": "api",
                        "source_path": "api/payment-api",
                        "build_strategy": "maven",
                        "compose_service": "payment-api",
                        "container_port": 8080,
                        "depends_on": ["postgres"],
                    },
                    {
                        "key": "search-api",
                        "name": "Search API",
                        "kind": "api",
                        "source_path": "api/search-api",
                        "build_strategy": "maven",
                        "compose_service": "search-api",
                        "container_port": 8080,
                        "depends_on": ["elasticsearch"],
                    },
                    {
                        "key": "admin-ui",
                        "name": "Admin UI",
                        "kind": "website",
                        "source_path": "web/admin-ui",
                        "build_strategy": "npm",
                        "compose_service": "admin-ui",
                        "container_port": 4173,
                        "depends_on": ["identity-api", "payment-api"],
                    },
                    {
                        "key": "customer-ui",
                        "name": "Customer UI",
                        "kind": "website",
                        "source_path": "web/customer-ui",
                        "build_strategy": "npm",
                        "compose_service": "customer-ui",
                        "container_port": 4173,
                        "depends_on": ["identity-api"],
                    },
                ],
                "routes": [
                    {"service_key": "identity-api", "visibility": "public"},
                    {"service_key": "payment-api", "visibility": "public"},
                    {"service_key": "search-api", "visibility": "public"},
                    {"service_key": "admin-ui", "visibility": "public"},
                    {"service_key": "customer-ui", "visibility": "public"},
                ],
                "resources": [
                    {
                        "key": "postgres",
                        "kind": "postgres",
                        "name": "postgres",
                        "config": {"compose_service": "postgres", "service_type": "postgres", "source": "deployment_planner"},
                    },
                    {
                        "key": "elasticsearch",
                        "kind": "elasticsearch",
                        "name": "elasticsearch",
                        "config": {
                            "compose_service": "elasticsearch",
                            "service_type": "elasticsearch",
                            "source": "deployment_planner",
                        },
                    },
                    {
                        "key": "activemq",
                        "kind": "activemq",
                        "name": "activemq",
                        "config": {"compose_service": "activemq", "service_type": "activemq", "source": "deployment_planner"},
                    },
                ],
                "volumes": [
                    {
                        "key": "postgres-data",
                        "type": "persistent",
                        "name": "postgres-data",
                        "config": {
                            "compose_volume": "postgres-data",
                            "mount_path": "/var/lib/postgresql/data",
                            "source": "deployment_planner",
                        },
                    }
                ],
                "compose_raw": """\
services:
  identity-api:
    build:
      context: ./api/identity-api
  payment-api:
    build:
      context: ./api/payment-api
  search-api:
    build:
      context: ./api/search-api
  admin-ui:
    build:
      context: ./web/admin-ui
      dockerfile_inline: |
        FROM node:20-alpine
        WORKDIR /app
        COPY . .
        RUN npm ci && npm run build
        CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]
  customer-ui:
    build:
      context: ./web/customer-ui
      dockerfile_inline: |
        FROM node:20-alpine
        WORKDIR /app
        COPY . .
        RUN npm ci && npm run build
        CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: app
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.10
  activemq:
    image: apache/activemq-classic:6.1.7
volumes:
  postgres-data: {}
""",
            }
        }

        with (
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload) as invoke,
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    prompt = invoke.call_args.kwargs["user_prompt"]
    assert "api/identity-api" in prompt
    assert "web/admin-ui" in prompt
    assert "infra" in prompt
    assert "api/shared-library" not in prompt
    assert "web/legacy-ui" not in prompt
    assert "machine-learning/analytics" in prompt
    prompt_payload = json.loads(prompt)
    analytics_candidate = next(
        candidate
        for candidate in prompt_payload["deterministic_repo_evidence"]
        if candidate["source_path"] == "machine-learning/analytics"
    )
    assert analytics_candidate["uses_private_base_image"] is True
    assert ".master-builder/deployments" not in prompt
    assert "stale-generated-service" not in prompt
    assert "temporalio/auto-setup" not in prompt
    assert result.app.source_path == "."
    assert result.app.slug == "production"
    assert result.app.build_strategy == "docker_compose"
    assert "RUN npx vite build" in result.app.deployment_config["generated_compose_raw"]
    assert "RUN npm ci && npm run build" not in result.app.deployment_config["generated_compose_raw"]
    assert "docker.elastic.co" not in result.app.deployment_config["generated_compose_raw"]
    resources = result.app.deployment_config["resources"]
    assert {resource["key"] for resource in resources} == {
        "postgres",
        "elasticsearch",
        "activemq",
    }
    services = result.app.deployment_config["services"]
    assert {service["key"] for service in services} == {
        "identity-api",
        "payment-api",
        "search-api",
        "admin-ui",
        "customer-ui",
    }
    assert all(service["public"] is True for service in services)
    assert result.app.deployment_config["volumes"] == planner_payload["deployment"]["volumes"]


def test_run_project_deployment_planning_rejects_runtime_invented_deployable_service_path() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "web" / "app" / "package.json", '{"name":"app","dependencies":{"vite":"^5.0.0"}}')
        _write(
            root / "machine-learning" / "analytics" / "Dockerfile",
            "FROM europe-west2-docker.pkg.dev/example/private/runtime\nCMD [\"python\", \"app.py\"]\n",
        )
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "app-web",
                        "name": "App Web",
                        "kind": "website",
                        "source_path": "web/app",
                        "build_strategy": "npm",
                        "compose_service": "app-web",
                        "container_port": 4173,
                        "depends_on": [],
                    },
                        {
                            "key": "analytics-api",
                            "name": "Analytics API",
                            "kind": "api",
                            "source_path": "machine-learning/invented",
                            "build_strategy": "dockerfile",
                            "compose_service": "analytics-api",
                        "container_port": 8080,
                        "depends_on": [],
                    },
                ],
                "routes": [
                    {"service_key": "app-web", "visibility": "public"},
                    {"service_key": "analytics-api", "visibility": "public"},
                ],
                "resources": [],
                "volumes": [],
                "compose_raw": """\
services:
  app-web:
    build:
      context: ./web/app
      dockerfile_inline: |
        FROM node:22-alpine
        WORKDIR /app
        COPY . .
        RUN npm ci && npm run build
        CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]
  analytics-api:
    build:
      context: ./machine-learning/invented
      dockerfile_inline: |
        FROM python:3.12-slim
        WORKDIR /app
        COPY . .
        CMD ["python", "app.py"]
""",
            }
        }

        with (
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            with pytest.raises(ValueError, match="undeclared deployment service source_path"):
                run_project_deployment_planning(
                    tenant=SimpleNamespace(tenant_id="tenant-1"),
                    project=SimpleNamespace(
                        project_id="project-1",
                        name="Bsktpay",
                        github_repository="https://github.com/example/bsktpay",
                    ),
                    checkout_path=str(root),
                    branch="main",
                    commit_sha="abcdef1234567890",
                    analysis_source="deployment_setup",
                    session=SimpleNamespace(),
                    settings=SimpleNamespace(),
                )


def test_run_project_deployment_planning_aligns_compose_volume_names_with_contract() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "web" / "package.json",
            '{"name":"web","scripts":{"build":"vite build","start":"vite --host 0.0.0.0"},"dependencies":{"vite":"^5.0.0"}}',
        )
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "app-web",
                        "name": "App Web",
                        "kind": "website",
                        "source_path": "web",
                        "build_strategy": "docker_compose",
                        "compose_service": "app-web",
                        "container_port": 3000,
                        "depends_on": ["elasticsearch"],
                    }
                ],
                "routes": [{"service_key": "app-web", "visibility": "public"}],
                "resources": [
                    {
                        "key": "elasticsearch",
                        "kind": "elasticsearch",
                        "name": "Elasticsearch",
                        "config": {
                            "compose_service": "elasticsearch",
                            "service_type": "elasticsearch",
                            "source": "deployment_planner",
                        },
                    }
                ],
                "volumes": [
                    {
                        "key": "elasticsearch-data",
                        "type": "persistent",
                        "name": "elasticsearch-data",
                        "config": {
                            "compose_volume": "db",
                            "mount_path": "/home/elasticsearch/data",
                            "source": "deployment_planner",
                        },
                    }
                ],
                "compose_raw": """\
services:
  app-web:
    image: nginx:alpine
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.10
    volumes:
      - db:/home/elasticsearch/data
volumes:
  db: {}
""",
            }
        }

        with (
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    volumes = result.app.deployment_config["volumes"]
    assert [volume["key"] for volume in volumes] == ["elasticsearch-data"]
    assert volumes[0]["config"]["compose_volume"] == "elasticsearch-data"
    compose_payload = yaml.safe_load(result.app.deployment_config["generated_compose_raw"])
    assert "db" not in compose_payload["volumes"]
    assert "elasticsearch-data" in compose_payload["volumes"]
    assert compose_payload["services"]["elasticsearch"]["volumes"] == [
        "elasticsearch-data:/home/elasticsearch/data"
    ]
    assert compose_payload["services"]["elasticsearch"]["image"] == "bitnamilegacy/elasticsearch:8"
    assert compose_payload["services"]["elasticsearch"]["environment"]["ELASTICSEARCH_ENABLE_SECURITY"] == "false"
    assert compose_payload["services"]["elasticsearch"]["healthcheck"]["test"] == [
        "CMD-SHELL",
        "curl -fsS http://localhost:9200/_cluster/health >/dev/null || exit 1",
    ]


def test_deployment_plan_rejects_admin_helper_resource_services() -> None:
    payload = {
        "deployment": {
            "name": "production",
            "services": [
                {
                    "key": "app-web",
                    "name": "App Web",
                    "kind": "website",
                    "source_path": "web/app",
                    "build_strategy": "npm",
                    "compose_service": "app-web",
                    "container_port": 3000,
                }
            ],
            "routes": [{"service_key": "app-web", "visibility": "public"}],
            "resources": [
                {
                    "key": "dejavu",
                    "kind": "service",
                    "name": "dejavu",
                    "config": {"compose_service": "dejavu", "service_type": "admin_ui"},
                }
            ],
            "compose_raw": """\
services:
  app-web:
    build: ./web/app
  dejavu:
    image: appbaseio/dejavu
""",
        }
    }

    with pytest.raises(Exception, match="resources.0.kind"):
        DeploymentPlannerResponse.model_validate(payload)


def test_deployment_plan_rejects_volume_inside_resources() -> None:
    payload = {
        "deployment": {
            "name": "production",
            "services": [
                {
                    "key": "app-web",
                    "name": "App Web",
                    "kind": "website",
                    "source_path": "web/app",
                    "build_strategy": "npm",
                    "compose_service": "app-web",
                    "container_port": 3000,
                }
            ],
            "routes": [{"service_key": "app-web", "visibility": "public"}],
            "resources": [
                {
                    "key": "app-data",
                    "kind": "volume",
                    "name": "app-data",
                    "config": {"compose_volume": "app-data", "mount_path": "/data"},
                }
            ],
            "compose_raw": """\
services:
  app-web:
    build: ./web/app
volumes:
  app-data: {}
""",
        }
    }

    with pytest.raises(Exception, match="resources.0.kind"):
        DeploymentPlannerResponse.model_validate(payload)


def test_deployment_plan_rejects_undeclared_compose_services() -> None:
    payload = {
        "deployment": {
            "name": "production",
            "services": [
                {
                    "key": "app-web",
                    "name": "App Web",
                    "kind": "website",
                    "source_path": "web/app",
                    "build_strategy": "npm",
                    "compose_service": "app-web",
                    "container_port": 3000,
                }
            ],
            "routes": [{"service_key": "app-web", "visibility": "public"}],
            "resources": [
                {
                    "key": "elasticsearch",
                    "kind": "elasticsearch",
                    "name": "elasticsearch",
                    "config": {"compose_service": "elasticsearch"},
                }
            ],
            "compose_raw": """\
services:
  app-web:
    build: ./web/app
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.10
  dejavu:
    image: appbaseio/dejavu
""",
        }
    }

    with pytest.raises(Exception, match="compose_raw contains undeclared service\\(s\\): dejavu"):
        DeploymentPlannerResponse.model_validate(payload)


def test_deployment_plan_rejects_remote_git_build_contexts() -> None:
    payload = {
        "deployment": {
            "name": "production",
            "services": [
                {
                    "key": "app-web",
                    "name": "App Web",
                    "kind": "website",
                    "source_path": "web/app",
                    "build_strategy": "npm",
                    "compose_service": "app-web",
                    "container_port": 3000,
                }
            ],
            "routes": [{"service_key": "app-web", "visibility": "public"}],
            "compose_raw": """\
services:
  app-web:
    build: https://github.com/example/repo.git#abcdef:web/app
""",
        }
    }

    with pytest.raises(Exception, match="non-repository build context"):
        DeploymentPlannerResponse.model_validate(payload)


def test_deployment_plan_rejects_unescaped_dockerfile_inline_variables() -> None:
    payload = {
        "deployment": {
            "name": "production",
            "services": [
                {
                    "key": "api",
                    "name": "API",
                    "kind": "api",
                    "source_path": "api",
                    "build_strategy": "maven",
                    "compose_service": "api",
                    "container_port": 8080,
                }
            ],
            "routes": [{"service_key": "api", "visibility": "public"}],
            "compose_raw": """\
services:
  api:
    build:
      context: ./api
      dockerfile_inline: |
        FROM eclipse-temurin:17-jre
        CMD ["sh", "-lc", "java -jar $JAR"]
""",
        }
    }

    with pytest.raises(Exception, match="unescaped shell variables"):
        DeploymentPlannerResponse.model_validate(payload)


def test_deployment_plan_accepts_json_escaped_compose_newlines() -> None:
    payload = {
        "deployment": {
            "name": "production",
            "services": [
                {
                    "key": "app-web",
                    "name": "App Web",
                    "kind": "website",
                    "source_path": "web/app",
                    "build_strategy": "npm",
                    "compose_service": "app-web",
                    "container_port": 3000,
                }
            ],
            "routes": [{"service_key": "app-web", "visibility": "public"}],
            "compose_raw": "services:\\n  app-web:\\n    build: ./web/app\\n",
        }
    }

    parsed = DeploymentPlannerResponse.model_validate(payload)

    assert parsed.deployment.compose_raw == "services:\n  app-web:\n    build: ./web/app"


def test_run_project_deployment_planning_rejects_private_registry_base_image_dockerfile() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "analytics" / "Dockerfile",
            """\
FROM europe-west2-docker.pkg.dev/bsktpay/bsktpay/analytics-image-deployment
CMD ["python", "main.py"]
""",
        )
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "analytics-api",
                        "name": "Analytics API",
                        "kind": "api",
                        "source_path": "analytics",
                        "build_strategy": "dockerfile",
                        "compose_service": "analytics-api",
                        "container_port": 8080,
                    }
                ],
                "routes": [{"service_key": "analytics-api", "visibility": "internal"}],
                "compose_raw": """\
services:
  analytics-api:
    build:
      context: ./analytics
      dockerfile: Dockerfile
""",
            }
        }
        with (
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload) as invoke,
        ):
            with pytest.raises(Exception, match="unsupported/private registry"):
                run_project_deployment_planning(
                    tenant=SimpleNamespace(tenant_id="tenant-1"),
                    project=SimpleNamespace(
                        project_id="project-1",
                        name="Bsktpay",
                        github_repository="https://github.com/example/bsktpay",
                    ),
                    checkout_path=str(root),
                    branch="main",
                    commit_sha="abcdef1234567890",
                    analysis_source="deployment_setup",
                    session=SimpleNamespace(),
                    settings=SimpleNamespace(),
                )
        prompt_payload = json.loads(invoke.call_args.kwargs["user_prompt"])
        evidence = prompt_payload["deterministic_repo_evidence"]
        analytics_candidate = next(candidate for candidate in evidence if candidate["source_path"] == "analytics")
        assert analytics_candidate["uses_private_base_image"] is True


def test_run_project_deployment_planning_accepts_public_docker_hub_tagged_base_image() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "api" / "pom.xml", "<project><artifactId>spring-boot-starter-web</artifactId></project>")
        _write(root / "web" / "package.json", '{"name":"web","dependencies":{"vite":"^5.0.0"}}')
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "customer-api",
                        "name": "Customer API",
                        "kind": "api",
                        "source_path": "api",
                        "build_strategy": "maven",
                        "compose_service": "customer-api",
                        "container_port": 8080,
                    },
                    {
                        "key": "web-app",
                        "name": "Web App",
                        "kind": "website",
                        "source_path": "web",
                        "build_strategy": "npm",
                        "compose_service": "web-app",
                        "container_port": 4173,
                    }
                ],
                "routes": [
                    {"service_key": "customer-api", "visibility": "public"},
                    {"service_key": "web-app", "visibility": "public"},
                ],
                "resources": [
                    {
                        "key": "postgres",
                        "kind": "postgres",
                        "name": "postgres",
                        "config": {"compose_service": "postgres"},
                    },
                    {
                        "key": "activemq",
                        "kind": "activemq",
                        "name": "activemq",
                        "config": {"compose_service": "activemq"},
                    },
                    {
                        "key": "kafka",
                        "kind": "kafka",
                        "name": "kafka",
                        "config": {"compose_service": "kafka"},
                    },
                    {
                        "key": "elasticsearch",
                        "kind": "elasticsearch",
                        "name": "elasticsearch",
                        "config": {"compose_service": "elasticsearch"},
                    },
                    {
                        "key": "mailpit",
                        "kind": "smtp",
                        "name": "mailpit",
                        "config": {"compose_service": "mailpit"},
                    },
                ],
                "compose_raw": """\
services:
  customer-api:
    build:
      context: ./api
      dockerfile_inline: |
        FROM maven:3.9.9-eclipse-temurin-17 AS build
        FROM eclipse-temurin:17-jre
        CMD ["java", "-version"]
  web-app:
    build:
      context: ./web
      dockerfile_inline: |
        FROM node:22-alpine
        WORKDIR /app
        CMD ["node", "-e", "require('http').createServer((_, res) => res.end('ok')).listen(4173)"]
    expose:
      - "4173"
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: bsktpay
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
  activemq:
    image: apache/activemq-classic:6.1.7
  kafka:
    image: apache/kafka:4.2.0
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.10
  mailpit:
    image: axllent/mailpit:v1.28.2
""",
            }
        }
        scan_candidates = (
            ProjectAppPreScanCandidate(
                name="compose",
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
                deployment_config={},
                analysis_source="deployment_setup",
                needs_generated_files=False,
                services_json=(
                    {"key": "customer-api", "source_path": "api"},
                    {"key": "web-app", "source_path": "web"},
                ),
            ),
        )

        with (
            patch("orchestrator.core.deployment_setup.planner.scan_repo_for_project_apps", return_value=scan_candidates),
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    assert result.plan.services[0].key == "customer-api"


def test_run_project_deployment_planning_normalizes_vite_runtime_dockerfile() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "web" / "app" / "package.json",
            """\
{"name": "app", "dependencies": {"vite": "^5.0.0"}, "scripts": {"build": "tsc -b && vite build"}}
""",
        )
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "app-web",
                        "name": "App Web",
                        "kind": "website",
                        "source_path": "web/app",
                        "build_strategy": "npm",
                        "compose_service": "app-web",
                        "container_port": 4173,
                    }
                ],
                "routes": [{"service_key": "app-web", "visibility": "public"}],
                "compose_raw": """\
services:
  app-web:
    build:
      context: .
      dockerfile_inline: |
        FROM node:22-alpine
        WORKDIR /app
        COPY . .
        RUN npm ci
        RUN npm run build
        CMD ["npm", "run", "preview", "--", "--host", "0.0.0.0", "--port", "4173"]
""",
            }
        }
        scan_candidates = (
            ProjectAppPreScanCandidate(
                name="compose",
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
                deployment_config={},
                analysis_source="deployment_setup",
                needs_generated_files=False,
                services_json=({"key": "app-web", "source_path": "web/app"},),
            ),
        )

        with (
            patch("orchestrator.core.deployment_setup.planner.scan_repo_for_project_apps", return_value=scan_candidates),
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    compose_raw = result.app.deployment_config["generated_compose_raw"]
    compose_payload = yaml.safe_load(compose_raw)
    assert compose_payload["services"]["app-web"]["build"]["context"] == "./web/app"
    assert "RUN npx vite build" in compose_raw
    assert "RUN npm run build" not in compose_raw
    assert 'CMD ["serve", "-s", "dist", "-l", "4173"]' in compose_raw


def test_run_project_deployment_planning_disables_nested_maven_docker_plugin() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "api" / "pom.xml", "<project><artifactId>customer-api</artifactId></project>")
        _write(root / "api" / "src" / "main" / "resources" / "application.properties", "spring.application.name=customer\n")
        _write(root / "web" / "package.json", '{"name":"web","dependencies":{"vite":"^5.0.0"}}')
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "customer-api",
                        "name": "Customer API",
                        "kind": "api",
                        "source_path": "api",
                        "build_strategy": "maven",
                        "compose_service": "customer-api",
                        "container_port": 8080,
                    },
                    {
                        "key": "web-app",
                        "name": "Web App",
                        "kind": "website",
                        "source_path": "web",
                        "build_strategy": "npm",
                        "compose_service": "web-app",
                        "container_port": 4173,
                    },
                ],
                "routes": [
                    {"service_key": "customer-api", "visibility": "public"},
                    {"service_key": "web-app", "visibility": "public"},
                ],
                "resources": [
                    {
                        "key": "postgres",
                        "kind": "postgres",
                        "name": "postgres",
                        "config": {"compose_service": "postgres"},
                    },
                    {
                        "key": "activemq",
                        "kind": "activemq",
                        "name": "activemq",
                        "config": {"compose_service": "activemq"},
                    },
                    {
                        "key": "kafka",
                        "kind": "kafka",
                        "name": "kafka",
                        "config": {"compose_service": "kafka"},
                    },
                    {
                        "key": "elasticsearch",
                        "kind": "elasticsearch",
                        "name": "elasticsearch",
                        "config": {"compose_service": "elasticsearch"},
                    },
                    {
                        "key": "mailpit",
                        "kind": "smtp",
                        "name": "mailpit",
                        "config": {"compose_service": "mailpit"},
                    },
                ],
                "compose_raw": """\
services:
  customer-api:
    build:
      context: ./api
      dockerfile_inline: |
        FROM maven:3.9.9-eclipse-temurin-17 AS build
        WORKDIR /workspace
        COPY . .
        RUN mvn -pl org.example.customer -am -DskipTests package
        FROM eclipse-temurin:17-jre-alpine
        RUN apk add --no-cache curl
        COPY --from=build /workspace/org.example.customer/target/*.jar /app/app.jar
        EXPOSE 8080 8081
        ENTRYPOINT java -jar /app/app.jar --spring.profiles.active=aws-prod
    expose:
      - "8080"
      - "8081"
    healthcheck:
      test:
        - "\\\"CMD-SHELL\\\""
        - "\\\"curl -f http://localhost:8081/actuator/health/ || exit 1\\\""
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_DB=bsktpay
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=postgres
  web-app:
    build:
      context: ./web
      dockerfile_inline: |
        FROM node:22-alpine AS build
        WORKDIR /app
        COPY package*.json ./
        RUN npm ci
        COPY . .
        RUN npm run build
        FROM node:22-alpine
        WORKDIR /app
        RUN npm install -g serve
        COPY --from=build /app/dist ./dist
        EXPOSE 4173
        CMD ["serve", "-s", "dist", "-l", "4173"]
  activemq:
    image: apache/activemq-classic:6.1.7
  kafka:
    image: apache/kafka:4.2.0
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.19.10
  mailpit:
    image: axllent/mailpit:v1.28.2
    environment:
      MP_SMTP_BIND_ADDR: 0.0.0.0:1025
""",
            }
        }
        scan_candidates = (
            ProjectAppPreScanCandidate(
                name="compose",
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
                deployment_config={},
                analysis_source="deployment_setup",
                needs_generated_files=False,
                services_json=(
                    {"key": "customer-api", "source_path": "api"},
                    {"key": "web-app", "source_path": "web"},
                ),
            ),
        )

        with (
            patch("orchestrator.core.deployment_setup.planner.scan_repo_for_project_apps", return_value=scan_candidates),
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    compose_raw = result.app.deployment_config["generated_compose_raw"]
    compose_payload = yaml.safe_load(compose_raw)
    dockerfile_inline = compose_payload["services"]["customer-api"]["build"]["dockerfile_inline"]
    assert compose_payload["services"]["customer-api"]["build"]["context"] == "./api"
    assert "RUN mvn -pl . -am -DskipTests -DskipDocker=true -Dmaven.test.skip=true package" in dockerfile_inline
    assert "FROM eclipse-temurin:17-jre\n" in dockerfile_inline
    assert "eclipse-temurin:17-jre-alpine" not in dockerfile_inline
    assert "RUN apk add --no-cache curl" not in dockerfile_inline
    assert "apt-get install -y --no-install-recommends curl" in dockerfile_inline
    assert (
        "RUN find /workspace/target -maxdepth 1 -type f -name '*.jar' "
        "! -name 'original-*.jar' -exec cp {} /tmp/app.jar \\; -quit"
    ) in dockerfile_inline
    assert "/tmp/master-builder-config" in dockerfile_inline
    assert "spring[.]config[.]import" in dockerfile_inline
    assert "COPY --from=build /tmp/master-builder-config /app/master-builder-config" in dockerfile_inline
    assert "RUN cat > /app/master-builder-logback.xml <<'XML'" in dockerfile_inline
    assert "COPY --from=build /tmp/app.jar /app/app.jar" in dockerfile_inline
    assert "--spring.profiles.active=aws-prod" not in dockerfile_inline
    environment = compose_payload["services"]["customer-api"]["environment"]
    assert environment["POSTGRES_DB_URL"] == "jdbc:postgresql://postgres:5432/bsktpay"
    assert environment["SPRING_DATASOURCE_URL"] == "jdbc:postgresql://postgres:5432/bsktpay"
    assert environment["SPRING_DATASOURCE_USERNAME"] == "postgres"
    assert environment["SPRING_DATASOURCE_PASSWORD"] == "postgres"
    assert environment["TENANT_DATASOURCE_USERNAME"] == "postgres"
    assert environment["TENANT_DATASOURCE_PASSWORD"] == "postgres"
    assert environment["SPRING_PROFILES_ACTIVE"] == "aws-sandbox"
    assert environment["SPRING_CONFIG_LOCATION"] == "file:/app/master-builder-config/"
    assert environment["LOGGING_CONFIG"] == "file:/app/master-builder-logback.xml"
    assert environment["SPRING_CLOUD_GCP_ENABLED"] == "false"
    assert environment["SPRING_CLOUD_GCP_CORE_ENABLED"] == "false"
    assert environment["SPRING_CLOUD_GCP_FIRESTORE_ENABLED"] == "false"
    assert environment["SPRING_CLOUD_GCP_PUBSUB_ENABLED"] == "false"
    assert environment["SPRING_CLOUD_GCP_SECRETMANAGER_ENABLED"] == "false"
    assert environment["SPRING_CLOUD_GCP_SQL_ENABLED"] == "false"
    assert environment["SPRING_CLOUD_GCP_STORAGE_ENABLED"] == "false"
    assert environment["SPRING_CLOUD_GCP_TRACE_ENABLED"] == "false"
    assert environment["BASE_ENVIRONMENT"] == "sandbox"
    assert environment["TWILIO_ENABLE"] == "false"
    assert environment["BASE_MAIL_LOCATION"] == "pickup"
    assert "SPRING_FLYWAY_SCHEMAS" not in environment
    assert "SPRING_FLYWAY_DEFAULT_SCHEMA" not in environment
    assert "SPRING_JPA_PROPERTIES_HIBERNATE_DEFAULT_SCHEMA" not in environment
    assert environment["SPRING_JPA_HIBERNATE_DDL_AUTO"] == "none"
    assert environment["SPRING_FLYWAY_POSTGRESQL_TRANSACTIONAL_LOCK"] == "false"
    assert environment["BASE_WEB_PUBLIC_IMAGE_URL"] == "http://web-app:4173"
    assert environment["SPRING_ACTIVEMQ_BROKER_URL"] == "tcp://activemq:61616?wireFormat.maxInactivityDuration=0"
    assert environment["SPRING_KAFKA_BOOTSTRAP_SERVERS"] == "kafka:9092"
    assert environment["SPRING_ELASTICSEARCH_URIS"] == "http://elasticsearch:9200"
    assert environment["SPRING_ELASTICSEARCH_HOST"] == "elasticsearch"
    assert environment["AUTH0_AUDIENCE"] == ""
    assert environment["AUTH0_DOMAIN"] == ""
    assert environment["spring.mail.host"] == "mailpit"
    assert environment["spring.mail.port"] == "1025"
    assert environment["base.email.errorTo"] == "errors@localhost"
    assert environment["BASE_EMAIL_ERROR_TO"] == "errors@localhost"
    assert environment["SERVER_PORT"] == "8080"
    assert environment["MANAGEMENT_SERVER_PORT"] == "8081"
    healthcheck_test = compose_payload["services"]["customer-api"]["healthcheck"]["test"]
    assert healthcheck_test == ["CMD-SHELL", "curl -f http://localhost:8081/actuator/health/ || exit 1"]
    postgres_service = compose_payload["services"]["postgres"]
    assert postgres_service["build"]["context"] == "."
    postgres_dockerfile = postgres_service["build"]["dockerfile_inline"]
    assert "FROM postgis/postgis:16-3.4" in postgres_dockerfile
    assert "create_role_if_missing postgres" in postgres_dockerfile
    assert "create_role_if_missing postgres_customer" in postgres_dockerfile
    assert "create_role_if_missing postgres_tenant" in postgres_dockerfile
    assert "GRANT CONNECT ON DATABASE \"$$POSTGRES_DB\" TO postgres" in postgres_dockerfile
    assert "DO $$" not in postgres_dockerfile
    assert postgres_service["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -h localhost -U $$POSTGRES_USER -d $$POSTGRES_DB",
    ]
    assert "image" not in postgres_service


def test_run_project_deployment_planning_adds_maven_healthcheck_for_health_gated_dependencies() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(
            root / "api" / "pom.xml",
            """\
<project>
  <modules>
    <module>management</module>
    <module>customer</module>
  </modules>
</project>
""",
        )
        _write(root / "api" / "management" / "pom.xml", "<project><artifactId>management</artifactId></project>")
        _write(root / "api" / "management" / "src" / "main" / "resources" / "application.properties", "spring.application.name=management\n")
        _write(root / "api" / "customer" / "pom.xml", "<project><artifactId>customer</artifactId></project>")
        _write(root / "api" / "customer" / "src" / "main" / "resources" / "application.properties", "spring.application.name=customer\n")
        _write(
            root / "api" / "customer" / "src" / "main" / "java" / "com" / "example" / "customer" / "SpringBootApp.java",
            """\
package com.example.customer;

import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication(scanBasePackages = {"com.example.customer"})
public class SpringBootApp {}
""",
        )
        _write(
            root / "api" / "customer" / "src" / "main" / "java" / "com" / "example" / "customer" / "WorkflowSessionService.java",
            """\
package com.example.customer;

import com.example.engine.ExpressionConditionalEngine;

public class WorkflowSessionService {
    private final ExpressionConditionalEngine engine;
    public WorkflowSessionService(ExpressionConditionalEngine engine) {
        this.engine = engine;
    }
}
""",
        )
        _write(
            root / "api" / "engine" / "src" / "main" / "java" / "com" / "example" / "engine" / "ExpressionConditionalEngine.java",
            """\
package com.example.engine;

import org.springframework.stereotype.Service;

@Service
public class ExpressionConditionalEngine {}
""",
        )
        _write(root / "web" / "package.json", '{"name":"web","dependencies":{"vite":"^5.0.0"}}')
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "management-api",
                        "name": "Management API",
                        "kind": "api",
                        "source_path": "api/management",
                        "build_strategy": "maven",
                        "compose_service": "management-api",
                        "container_port": 8080,
                    },
                    {
                        "key": "customer-api",
                        "name": "Customer API",
                        "kind": "api",
                        "source_path": "api/customer",
                        "build_strategy": "maven",
                        "compose_service": "customer-api",
                        "container_port": 8080,
                        "depends_on": ["management-api"],
                    },
                    {
                        "key": "web-app",
                        "name": "Web App",
                        "kind": "website",
                        "source_path": "web",
                        "build_strategy": "npm",
                        "compose_service": "web-app",
                        "container_port": 4173,
                    },
                ],
                "routes": [
                    {"service_key": "management-api", "visibility": "public"},
                    {"service_key": "customer-api", "visibility": "public"},
                    {"service_key": "web-app", "visibility": "public"},
                ],
                "resources": [
                    {
                        "key": "postgres",
                        "kind": "postgres",
                        "name": "postgres",
                        "config": {"compose_service": "postgres"},
                    }
                ],
                "compose_raw": """\
services:
  management-api:
    build:
      context: ./api
      dockerfile_inline: |
        FROM maven:3.9.11-eclipse-temurin-17 AS build
        WORKDIR /workspace
        COPY . .
        RUN mvn -q -DskipTests -pl management -am package
        RUN cp /workspace/management/target/app.jar /tmp/app.jar
        FROM eclipse-temurin:17-jre
        WORKDIR /app
        COPY --from=build /tmp/app.jar /app/app.jar
        EXPOSE 8080
        ENTRYPOINT java -jar /app/app.jar
    expose:
      - "8080"
  customer-api:
    build:
      context: ./api
      dockerfile_inline: |
        FROM maven:3.9.11-eclipse-temurin-17 AS build
        WORKDIR /workspace
        COPY . .
        RUN mvn -q -DskipTests -pl customer -am package
        RUN cp /workspace/customer/target/app.jar /tmp/app.jar
        FROM eclipse-temurin:17-jre
        WORKDIR /app
        COPY --from=build /tmp/app.jar /app/app.jar
        EXPOSE 8080
        ENTRYPOINT java -jar /app/app.jar
    depends_on:
      - management-api
    expose:
      - "8080"
  postgres:
    image: postgres:16
    environment:
      - POSTGRES_DB=bsktpay
      - POSTGRES_USER=postgres
      - POSTGRES_PASSWORD=postgres
  web-app:
    build:
      context: ./web
      dockerfile_inline: |
        FROM node:22-alpine AS build
        WORKDIR /app
        COPY package*.json ./
        RUN npm ci
        COPY . .
        RUN npm run build
        FROM node:22-alpine
        WORKDIR /app
        RUN npm install -g serve
        COPY --from=build /app/dist ./dist
        EXPOSE 4173
        CMD ["serve", "-s", "dist", "-l", "4173"]
""",
            }
        }
        scan_candidates = (
            ProjectAppPreScanCandidate(
                name="compose",
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
                deployment_config={},
                analysis_source="deployment_setup",
                needs_generated_files=False,
                services_json=(
                    {"key": "management-api", "source_path": "api/management"},
                    {"key": "customer-api", "source_path": "api/customer"},
                    {"key": "web-app", "source_path": "web"},
                ),
            ),
        )

        with (
            patch("orchestrator.core.deployment_setup.planner.scan_repo_for_project_apps", return_value=scan_candidates),
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    compose_payload = yaml.safe_load(result.app.deployment_config["generated_compose_raw"])
    management_service = compose_payload["services"]["management-api"]
    customer_service = compose_payload["services"]["customer-api"]
    assert management_service["healthcheck"]["test"] == [
        "CMD-SHELL",
        "curl -f http://localhost:8080/actuator/health/ || exit 1",
    ]
    assert management_service["depends_on"]["postgres"] == {"condition": "service_healthy"}
    assert customer_service["depends_on"]["management-api"] == {"condition": "service_healthy"}
    assert customer_service["depends_on"]["postgres"] == {"condition": "service_healthy"}
    assert "apt-get install -y --no-install-recommends curl" in management_service["build"]["dockerfile_inline"]
    assert management_service["build"]["dockerfile_inline"].count("apt-get install -y --no-install-recommends curl") == 1
    assert "/app/master-builder-config" in management_service["build"]["dockerfile_inline"]
    assert "/app/master-builder-logback.xml" in management_service["build"]["dockerfile_inline"]
    assert "apt-get install -y --no-install-recommends curl" in customer_service["build"]["dockerfile_inline"]
    assert "/app/master-builder-config" in customer_service["build"]["dockerfile_inline"]
    assert customer_service["environment"]["SPRING_MAIN_SOURCES"] == "com.example.engine.ExpressionConditionalEngine"


def test_run_project_deployment_planning_preserves_python_projectroot_marker() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "analytics" / ".projectroot", "")
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "analytics-api",
                        "name": "Analytics API",
                        "kind": "api",
                        "source_path": "analytics",
                        "build_strategy": "dockerfile",
                        "compose_service": "analytics-api",
                        "container_port": 8080,
                    }
                ],
                "routes": [{"service_key": "analytics-api", "visibility": "internal"}],
                "compose_raw": """\
services:
  analytics-api:
    build:
      context: ./analytics
      dockerfile_inline: |
        FROM python:3.11-slim
        WORKDIR /app
        RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
        COPY requirements.txt /app/requirements.txt
        RUN pip install --no-cache-dir -r /app/requirements.txt && pip install --no-cache-dir gunicorn
        COPY src/ /app/
        CMD ["gunicorn", "-c", "gunicorn.conf.py", "main:APP"]
    healthcheck:
      test:
        - CMD-SHELL
        - curl -fsS http://localhost:8080/health/ || exit 1
""",
            }
        }

        scan_candidates = (_dockerfile_pre_scan_candidate("analytics"),)

        with (
            patch("orchestrator.core.deployment_setup.planner.scan_repo_for_project_apps", return_value=scan_candidates),
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    compose_payload = yaml.safe_load(result.app.deployment_config["generated_compose_raw"])
    dockerfile_inline = compose_payload["services"]["analytics-api"]["build"]["dockerfile_inline"]
    assert "COPY .projectroot /app/.projectroot\nCOPY src/ /app/" in dockerfile_inline
    assert "apt-get install -y --no-install-recommends curl tesseract-ocr" in dockerfile_inline


def test_run_project_deployment_planning_pins_legacy_langchain_for_legacy_imports() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "analytics" / "src" / "discovery" / "charts.py", "from langchain.utilities import SQLDatabase\n")
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "analytics-api",
                        "name": "Analytics API",
                        "kind": "api",
                        "source_path": "analytics",
                        "build_strategy": "dockerfile",
                        "compose_service": "analytics-api",
                        "container_port": 8080,
                    }
                ],
                "routes": [{"service_key": "analytics-api", "visibility": "internal"}],
                "compose_raw": """\
services:
  analytics-api:
    build:
      context: ./analytics
      dockerfile_inline: |
        FROM python:3.11-slim
        WORKDIR /app
        COPY requirements.txt /app/requirements.txt
        RUN pip install --no-cache-dir -r /app/requirements.txt && pip install --no-cache-dir gunicorn
        COPY src/ /app/
        CMD ["gunicorn", "-c", "gunicorn.conf.py", "main:APP"]
""",
            }
        }

        scan_candidates = (_dockerfile_pre_scan_candidate("analytics"),)

        with (
            patch("orchestrator.core.deployment_setup.planner.scan_repo_for_project_apps", return_value=scan_candidates),
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    compose_payload = yaml.safe_load(result.app.deployment_config["generated_compose_raw"])
    dockerfile_inline = compose_payload["services"]["analytics-api"]["build"]["dockerfile_inline"]
    assert "langchain==0.2.17" in dockerfile_inline
    assert "langchain-community==0.2.19" in dockerfile_inline


def test_run_project_deployment_planning_pins_sklearn_to_model_artifact_version() -> None:
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write(root / "analytics" / "requirements.txt", "scikit-learn==1.5.0\n")
        _write(
            root / "analytics" / "Models" / "classifier.sav",
            "_sklearn_version\x94\x8c\x051.2.2\x94ub",
        )
        planner_payload = {
            "deployment": {
                "name": "production",
                "services": [
                    {
                        "key": "analytics-api",
                        "name": "Analytics API",
                        "kind": "api",
                        "source_path": "analytics",
                        "build_strategy": "dockerfile",
                        "compose_service": "analytics-api",
                        "container_port": 8080,
                    }
                ],
                "routes": [{"service_key": "analytics-api", "visibility": "internal"}],
                "compose_raw": """\
services:
  analytics-api:
    build:
      context: ./analytics
      dockerfile_inline: |
        FROM python:3.11-slim
        WORKDIR /app
        COPY requirements.txt /app/requirements.txt
        RUN pip install --no-cache-dir -r /app/requirements.txt && pip install --no-cache-dir gunicorn
        COPY src/ /app/
        CMD ["gunicorn", "-c", "gunicorn.conf.py", "main:APP"]
""",
            }
        }

        scan_candidates = (_dockerfile_pre_scan_candidate("analytics"),)

        with (
            patch("orchestrator.core.deployment_setup.planner.scan_repo_for_project_apps", return_value=scan_candidates),
            patch("orchestrator.core.deployment_setup.planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch("orchestrator.core.deployment_setup.planner.invoke_runtime_json", return_value=planner_payload),
        ):
            result = run_project_deployment_planning(
                tenant=SimpleNamespace(tenant_id="tenant-1"),
                project=SimpleNamespace(
                    project_id="project-1",
                    name="Bsktpay",
                    github_repository="https://github.com/example/bsktpay",
                ),
                checkout_path=str(root),
                branch="main",
                commit_sha="abcdef1234567890",
                analysis_source="deployment_setup",
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
            )

    compose_payload = yaml.safe_load(result.app.deployment_config["generated_compose_raw"])
    dockerfile_inline = compose_payload["services"]["analytics-api"]["build"]["dockerfile_inline"]
    assert (
        "RUN pip install --no-cache-dir --force-reinstall "
        "numpy==1.26.4 scipy==1.11.4 scikit-learn==1.2.2"
    ) in dockerfile_inline


def test_deployment_plan_rejects_host_source_bind_mounts() -> None:
    payload = {
        "deployment": {
            "name": "production",
            "services": [
                {
                    "key": "app-web",
                    "name": "App Web",
                    "kind": "website",
                    "source_path": "web/app",
                    "build_strategy": "npm",
                    "compose_service": "app-web",
                    "container_port": 3000,
                }
            ],
            "routes": [{"service_key": "app-web", "visibility": "public"}],
            "compose_raw": """\
services:
  app-web:
    image: node:20-alpine
    volumes:
      - ./web/app:/app
""",
        }
    }

    with pytest.raises(Exception, match="host-source bind mount"):
        DeploymentPlannerResponse.model_validate(payload)


def test_normalize_project_app_planner_output_rejects_traversal_source_paths() -> None:
    with pytest.raises(Exception, match="source_path must not traverse outside the repository"):
        normalize_project_app_planner_output(
            pre_scan_candidates=(),
            runtime_payload={
                "apps": [
                    {
                        "name": "bad",
                        "source_path": "../secrets",
                        "build_strategy": "nixpacks",
                        "port": 3000,
                        "healthcheck": None,
                        "resources": [],
                        "env": {},
                        "secrets": {},
                        "needs_generated_files": False,
                    }
                ]
            },
            analysis_source="manual_analyze",
        )


def test_normalize_project_app_planner_output_rejects_runtime_invented_source_paths() -> None:
    pre_scan_candidates = (
        SimpleNamespace(
            name="api-service",
            source_path=".",
            build_strategy="dockerfile",
            detected_runtime="java",
            detected_language="java",
            detection_confidence=0.9,
            exposed_port=8080,
            healthcheck=None,
            start_command=None,
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=False,
            resources_json=(),
            env_json={},
            secret_json={},
        ),
    )

    with pytest.raises(ValueError, match="undeclared source_path"):
        normalize_project_app_planner_output(
            pre_scan_candidates=pre_scan_candidates,
            runtime_payload={
                "apps": [
                    {
                        "name": "payment",
                        "source_path": "api/org.bsktpay.data/src/main/java/org/bsktpay/data/models/rest/payment",
                        "build_strategy": "nixpacks",
                        "port": None,
                        "healthcheck": None,
                        "resources": [],
                        "env": {},
                        "secrets": {},
                        "needs_generated_files": True,
                    }
                ]
            },
            analysis_source="manual_analyze",
        )


def test_analysis_persistence_does_not_downgrade_live_app_status() -> None:
    with TemporaryDirectory() as tmp:
        database_url = f"sqlite:///{tmp}/project_app_planner.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = database_url
        reset_db_engine_cache()
        run_migrations(database_url=database_url)
        session_factory = create_session_factory(database_url=database_url)
        now = datetime.now(timezone.utc)

        with session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant 1",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config=None,
                    deployment_plane_config={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project 1",
                    github_repository="https://github.com/example/repo",
                    jira_project_key="TP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ProjectApp(
                    app_id=ProjectAppNormalizedCandidate._app_id(
                        tenant_id="tenant-1",
                        project_id="project-1",
                        source_path="api/docker",
                    ),
                    tenant_id="tenant-1",
                    project_id="project-1",
                    name="align",
                    slug="align",
                    source_path="api/docker",
                    detection_confidence=0.7,
                    detected_runtime="docker",
                    detected_language=None,
                    analysis_source="manual_analyze",
                    build_strategy="docker_compose",
                    exposed_port=61616,
                    healthcheck=None,
                    start_command=None,
                    env_schema_json={},
                    secret_schema_json={},
                    deployment_config={},
                    status="live",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ProjectAppAnalysisRun(
                    run_id="analysis-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    status="completed",
                    planner_version="planner-v1",
                    request_payload={},
                    result_payload={},
                    error=None,
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            candidate = ProjectAppNormalizedCandidate(
                name="docker",
                slug="docker",
                source_path="api/docker",
                build_strategy="docker_compose",
                detected_runtime="docker",
                detected_language=None,
                detection_confidence=0.95,
                exposed_port=9159,
                healthcheck=None,
                start_command=None,
                env_schema_json={},
                secret_schema_json={},
                deployment_config={"source_strategy": "docker_compose"},
                analysis_source="manual_analyze",
                needs_generated_files=False,
            )

            persist_project_app_analysis_result(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                analysis_run_id="analysis-1",
                apps=(candidate,),
                raw_planner_result_json={"analysis_mode": "agent_deployment_plan"},
                analysis_source="manual_analyze",
                planner_version="planner-v1",
                now=now,
            )
            app = session.get(ProjectApp, candidate.to_project_app_kwargs(tenant_id="tenant-1", project_id="project-1")["app_id"])

        assert app is not None
        assert app.name == "align"
        assert app.slug == "align"
        assert app.status == "live"
        assert app.exposed_port == 9159
        reset_db_engine_cache()
