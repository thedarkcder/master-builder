from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.project_app_artifact_pr_runtime import create_project_app_artifact_pr
from orchestrator.core.project_app_planner import (
    ProjectAppAnalysisResult,
    ProjectAppAnalysisRunMetadata,
    ProjectAppNormalizedCandidate,
)


def _git(repo_dir: Path, *args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or f"git {' '.join(args)} failed")
    return process.stdout


def _init_repo(root: Path) -> Path:
    repo_dir = root / "repo"
    remote_dir = root / "remote.git"
    _git(root, "init", "--bare", str(remote_dir))
    _git(root, "init", str(repo_dir))
    _git(repo_dir, "config", "user.email", "codex@example.com")
    _git(repo_dir, "config", "user.name", "Codex")
    (repo_dir / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-m", "init")
    _git(repo_dir, "branch", "-M", "main")
    _git(repo_dir, "remote", "add", "origin", str(remote_dir))
    _git(repo_dir, "push", "-u", "origin", "main")
    _git(remote_dir, "symbolic-ref", "HEAD", "refs/heads/main")
    return repo_dir


def _analysis_result(candidate: ProjectAppNormalizedCandidate) -> ProjectAppAnalysisResult:
    return ProjectAppAnalysisResult(
        apps=(candidate,),
        metadata=ProjectAppAnalysisRunMetadata(
            tenant_id="tenant-1",
            project_id="project-1",
            checkout_path="",
            analysis_source="manual_analyze",
            planner_version="planner-v1",
            pre_scan_count=1,
            runtime_count=1,
            normalized_count=1,
            raw_planner_result_json={"apps": [candidate.to_result_json()]},
        ),
    )


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_create_project_app_artifact_pr_generates_missing_dockerfile_and_opens_pr() -> None:
    with TemporaryDirectory() as tmp:
        repo_dir = _init_repo(Path(tmp))
        candidate = ProjectAppNormalizedCandidate(
            name="api-service",
            source_path=".",
            build_strategy="dockerfile",
            detected_runtime="python",
            detected_language="python",
            detection_confidence=0.9,
            exposed_port=8000,
            healthcheck=None,
            start_command="uvicorn app.main:app --host 0.0.0.0 --port 8000",
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=True,
            resources_json=(),
            env_json={},
            secret_json={},
            slug="api-service",
        )
        captured = {}

        class _FakeClient:
            def get_installation_token(self) -> str:
                return "token-123"

            def find_open_pull_request(self, **_kwargs):  # noqa: ANN003
                return None

            def create_pull_request(self, **kwargs):  # noqa: ANN003
                captured.update(kwargs)
                return SimpleNamespace(number=17, html_url="https://github.com/example/repo/pull/17")

        with (
            patch(
                "orchestrator.core.project_app_artifact_pr_runtime.github_client_from_tenant_config",
                return_value=_FakeClient(),
            ),
            patch(
                "orchestrator.core.project_app_artifact_pr_runtime._resolve_remote_default_branch",
                return_value="main",
            ),
            patch(
                "orchestrator.core.project_app_artifact_pr_runtime._sync_local_base_branch_to_origin",
                side_effect=lambda *args, **kwargs: None,
            ),
        ):
            result = create_project_app_artifact_pr(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", name="Project 1", github_repository="https://github.com/example/repo"),
                checkout_path=str(repo_dir),
                analysis_run_id="analysis-run-1",
                analysis_result=_analysis_result(candidate),
            )

        assert result is not None
        assert result.pr_number == 17
        assert result.head_branch == "project-app-artifacts/analysis-run-1"
        assert result.base_branch == "main"
        assert result.generated_files == ("Dockerfile",)
        dockerfile = (repo_dir / "Dockerfile").read_text(encoding="utf-8")
        assert "FROM python:3.12-slim" in dockerfile
        assert 'CMD ["sh", "-lc", "uvicorn app.main:app --host 0.0.0.0 --port 8000"]' in dockerfile
        assert captured["head_branch"] == "project-app-artifacts/analysis-run-1"
        assert captured["base_branch"] == "main"
        assert captured["repo_full_name"] == "example/repo"
        assert "analysis-run-1" in captured["body"]
        assert "- Dockerfile" in captured["body"]


def test_create_project_app_artifact_pr_returns_existing_pr_without_writing_files() -> None:
    with TemporaryDirectory() as tmp:
        repo_dir = _init_repo(Path(tmp))
        candidate = ProjectAppNormalizedCandidate(
            name="api-service",
            source_path=".",
            build_strategy="dockerfile",
            detected_runtime="python",
            detected_language="python",
            detection_confidence=0.9,
            exposed_port=8000,
            healthcheck=None,
            start_command="python app.py",
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=True,
            resources_json=(),
            env_json={},
            secret_json={},
            slug="api-service",
        )

        class _FakeClient:
            def get_installation_token(self) -> str:
                return "token-123"

            def find_open_pull_request(self, **_kwargs):  # noqa: ANN003
                return SimpleNamespace(number=99, html_url="https://github.com/example/repo/pull/99")

            def create_pull_request(self, **_kwargs):  # noqa: ANN003
                raise AssertionError("create_pull_request should not be called when an open PR exists")

        with patch(
            "orchestrator.core.project_app_artifact_pr_runtime.github_client_from_tenant_config",
            return_value=_FakeClient(),
        ):
            result = create_project_app_artifact_pr(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", name="Project 1", github_repository="https://github.com/example/repo"),
                checkout_path=str(repo_dir),
                analysis_run_id="analysis-run-1",
                analysis_result=_analysis_result(candidate),
            )

        assert result is not None
        assert result.pr_number == 99
        assert result.head_branch == "project-app-artifacts/analysis-run-1"
        assert result.generated_files == ("Dockerfile",)
        assert not (repo_dir / "Dockerfile").exists()
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_create_project_app_artifact_pr_generates_compose_file_for_docker_compose_strategy() -> None:
    with TemporaryDirectory() as tmp:
        repo_dir = _init_repo(Path(tmp))
        candidate = ProjectAppNormalizedCandidate(
            name="frontend",
            source_path="frontend",
            build_strategy="docker_compose",
            detected_runtime="javascript",
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
            slug="frontend",
        )

        class _FakeClient:
            def get_installation_token(self) -> str:
                return "token-123"

            def find_open_pull_request(self, **_kwargs):  # noqa: ANN003
                return None

            def create_pull_request(self, **_kwargs):  # noqa: ANN003
                return SimpleNamespace(number=21, html_url="https://github.com/example/repo/pull/21")

        with patch(
            "orchestrator.core.project_app_artifact_pr_runtime.github_client_from_tenant_config",
            return_value=_FakeClient(),
        ):
            result = create_project_app_artifact_pr(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", name="Project 1", github_repository="https://github.com/example/repo"),
                checkout_path=str(repo_dir),
                analysis_run_id="analysis-run-2",
                analysis_result=_analysis_result(candidate),
            )

        assert result is not None
        compose_file = (repo_dir / "frontend" / "docker-compose.yml").read_text(encoding="utf-8")
        assert "image: node:20-slim" in compose_file
        assert 'command: ["sh", "-lc", "npm run start"]' in compose_file
        assert '3000:3000' in compose_file


def test_create_project_app_artifact_pr_does_not_regenerate_or_commit_when_files_already_exist() -> None:
    with TemporaryDirectory() as tmp:
        repo_dir = _init_repo(Path(tmp))
        _write(repo_dir / "Dockerfile", "FROM python:3.12-slim\nEXPOSE 8000\n")
        _write(repo_dir / "frontend" / "docker-compose.yml", "services:\n  app:\n    image: python:3.12-slim\n")
        _git(repo_dir, "add", "-A")
        _git(repo_dir, "commit", "-m", "add generated files")
        _git(repo_dir, "push", "-u", "origin", "main")
        head_before = _git(repo_dir, "rev-parse", "HEAD").strip()

        candidate_api = ProjectAppNormalizedCandidate(
            name="api-service",
            source_path=".",
            build_strategy="dockerfile",
            detected_runtime="python",
            detected_language="python",
            detection_confidence=0.9,
            exposed_port=8000,
            healthcheck=None,
            start_command="python app.py",
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=True,
            resources_json=(),
            env_json={},
            secret_json={},
            slug="api-service",
        )
        candidate_frontend = ProjectAppNormalizedCandidate(
            name="frontend",
            source_path="frontend",
            build_strategy="docker_compose",
            detected_runtime="javascript",
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
            slug="frontend",
        )
        analysis_result = ProjectAppAnalysisResult(
            apps=(candidate_api, candidate_frontend),
            metadata=ProjectAppAnalysisRunMetadata(
                tenant_id="tenant-1",
                project_id="project-1",
                checkout_path=str(repo_dir),
                analysis_source="manual_analyze",
                planner_version="planner-v1",
                pre_scan_count=2,
                runtime_count=2,
                normalized_count=2,
                raw_planner_result_json={
                    "apps": [
                        {
                            "name": candidate_api.name,
                            "source_path": candidate_api.source_path,
                            "build_strategy": candidate_api.build_strategy,
                            "port": candidate_api.exposed_port,
                            "healthcheck": candidate_api.healthcheck,
                            "resources": [],
                            "env": {},
                            "secrets": {},
                            "needs_generated_files": True,
                        },
                        {
                            "name": candidate_frontend.name,
                            "source_path": candidate_frontend.source_path,
                            "build_strategy": candidate_frontend.build_strategy,
                            "port": candidate_frontend.exposed_port,
                            "healthcheck": candidate_frontend.healthcheck,
                            "resources": [],
                            "env": {},
                            "secrets": {},
                            "needs_generated_files": True,
                        },
                    ]
                },
            ),
        )

        create_pull_request_calls = []

        class _FakeClient:
            def get_installation_token(self) -> str:
                return "token-123"

            def find_open_pull_request(self, **_kwargs):  # noqa: ANN003
                return None

            def create_pull_request(self, **kwargs):  # noqa: ANN003
                create_pull_request_calls.append(kwargs)
                return SimpleNamespace(number=21, html_url="https://github.com/example/repo/pull/21")

        with patch(
            "orchestrator.core.project_app_artifact_pr_runtime.github_client_from_tenant_config",
            return_value=_FakeClient(),
        ):
            result = create_project_app_artifact_pr(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", name="Project 1", github_repository="https://github.com/example/repo"),
                checkout_path=str(repo_dir),
                analysis_run_id="analysis-run-4",
                analysis_result=analysis_result,
            )

        assert result is None
        assert create_pull_request_calls == []
        assert _git(repo_dir, "rev-parse", "HEAD").strip() == head_before
        assert _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
        assert (repo_dir / "Dockerfile").read_text(encoding="utf-8") == "FROM python:3.12-slim\nEXPOSE 8000\n"
        assert (repo_dir / "frontend" / "docker-compose.yml").read_text(encoding="utf-8") == "services:\n  app:\n    image: python:3.12-slim\n"


def test_create_project_app_artifact_pr_ignores_nixpacks_candidates_without_generated_files() -> None:
    with TemporaryDirectory() as tmp:
        repo_dir = _init_repo(Path(tmp))
        candidate = ProjectAppNormalizedCandidate(
            name="backend",
            source_path=".",
            build_strategy="nixpacks",
            detected_runtime="python",
            detected_language="python",
            detection_confidence=0.7,
            exposed_port=8000,
            healthcheck=None,
            start_command="uvicorn app.main:app --host 0.0.0.0 --port 8000",
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=True,
            resources_json=(),
            env_json={},
            secret_json={},
            slug="backend",
        )
        create_pull_request_calls = []

        class _FakeClient:
            def get_installation_token(self) -> str:
                return "token-123"

            def find_open_pull_request(self, **_kwargs):  # noqa: ANN003
                return None

            def create_pull_request(self, **kwargs):  # noqa: ANN003
                create_pull_request_calls.append(kwargs)
                return SimpleNamespace(number=21, html_url="https://github.com/example/repo/pull/21")

        with patch(
            "orchestrator.core.project_app_artifact_pr_runtime.github_client_from_tenant_config",
            return_value=_FakeClient(),
        ):
            result = create_project_app_artifact_pr(
                session=SimpleNamespace(),
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant=SimpleNamespace(tenant_id="tenant-1", github_config={}),
                project=SimpleNamespace(project_id="project-1", name="Project 1", github_repository="https://github.com/example/repo"),
                checkout_path=str(repo_dir),
                analysis_run_id="analysis-run-3",
                analysis_result=_analysis_result(candidate),
            )

        assert result is None
        assert create_pull_request_calls == []
        assert not (repo_dir / "Dockerfile").exists()
        assert not (repo_dir / "docker-compose.yml").exists()
