from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.project_app_analysis_runtime import run_project_app_analysis
from orchestrator.core.project_app_planner import (
    normalize_project_app_planner_output,
    scan_repo_for_project_apps,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


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
