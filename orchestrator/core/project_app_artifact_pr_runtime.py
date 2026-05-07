from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sqlalchemy.orm import Session

from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.project_app_planner import (
    ProjectAppAnalysisResult,
    ProjectAppNormalizedCandidate,
)
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.github_app import GitHubApiError, github_client_from_tenant_config
from orchestrator.tools.repo_allowlist import normalize_repo_identifier


@dataclass(frozen=True)
class ProjectAppArtifactPRResult:
    pr_number: int
    pr_url: str
    head_branch: str
    base_branch: str
    generated_files: tuple[str, ...]

    def to_result_json(self) -> dict[str, object]:
        return {
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "head_branch": self.head_branch,
            "base_branch": self.base_branch,
            "generated_files": list(self.generated_files),
        }


@dataclass(frozen=True)
class _GeneratedFileSpec:
    path: str
    content: str


def create_project_app_artifact_pr(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    checkout_path: str,
    analysis_run_id: str,
    analysis_result: ProjectAppAnalysisResult,
) -> ProjectAppArtifactPRResult | None:
    repo_dir = Path(str(checkout_path or "").strip()).resolve()
    if not repo_dir.exists() or not repo_dir.is_dir():
        raise RuntimeError(f"Project app checkout path does not exist or is not a directory: {repo_dir}")

    github_repository = str(project.github_repository or "").strip()
    if not github_repository:
        raise RuntimeError("Project GitHub repository is required before generating artifact PRs")
    repo_full_name = _repo_full_name(github_repository)

    github_config = tenant.github_config or {}
    github_client = github_client_from_tenant_config(
        github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            encryption_key=settings.secrets_encryption_key,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )

    generated_specs = tuple(_build_generated_file_specs(analysis_result.apps))
    if not generated_specs:
        return None

    branch_name = _artifact_branch_name(analysis_run_id=analysis_run_id)
    token = github_client.get_installation_token()
    base_branch = _resolve_remote_default_branch(repo_dir, token=token)
    existing_pr = github_client.find_open_pull_request(
        repo_full_name=repo_full_name,
        head_branch=branch_name,
        base_branch=base_branch,
        limit=100,
    )
    if existing_pr is not None:
        return ProjectAppArtifactPRResult(
            pr_number=existing_pr.number,
            pr_url=existing_pr.html_url,
            head_branch=branch_name,
            base_branch=base_branch,
            generated_files=tuple(spec.path for spec in generated_specs),
        )

    missing_specs = tuple(spec for spec in generated_specs if not (repo_dir / spec.path).exists())
    if not missing_specs:
        return None

    _sync_local_base_branch_to_origin(repo_dir, base_branch=base_branch, token=token)
    _run_git(repo_dir, ["checkout", "-B", branch_name])

    generated_files = _write_generated_files(repo_dir=repo_dir, specs=missing_specs)

    _run_git(repo_dir, ["add", "-A"])
    staged_files = _run_git(repo_dir, ["diff", "--cached", "--name-only"]).strip().splitlines()
    if not any(item.strip() for item in staged_files):
        return None

    _ensure_local_commit_identity(repo_dir)
    commit_message = f"{project.project_id}: generate deploy artifacts ({analysis_run_id})"
    _run_git(repo_dir, ["commit", "-m", commit_message])
    _run_git(repo_dir, ["push", "-u", "origin", f"HEAD:{branch_name}"], token=token)

    try:
        pull_request = github_client.create_pull_request(
            repo_full_name=repo_full_name,
            github_repository=github_repository,
            title=f"{project.name}: generated deploy artifacts",
            head_branch=branch_name,
            base_branch=base_branch,
            body=_build_pull_request_body(
                project=project,
                analysis_run_id=analysis_run_id,
                generated_files=generated_files,
                base_branch=base_branch,
                branch_name=branch_name,
            ),
        )
    except (GitHubApiError, ValueError) as exc:
        raise RuntimeError(
            f"Unable to create artifact PR for project {project.project_id} analysis run {analysis_run_id}: {exc}"
        ) from exc

    return ProjectAppArtifactPRResult(
        pr_number=pull_request.number,
        pr_url=pull_request.html_url,
        head_branch=branch_name,
        base_branch=base_branch,
        generated_files=generated_files,
    )


def _build_generated_file_specs(apps: Iterable[ProjectAppNormalizedCandidate]) -> tuple[_GeneratedFileSpec, ...]:
    specs: list[_GeneratedFileSpec] = []
    for app in apps:
        if not app.needs_generated_files:
            continue
        if app.build_strategy == "dockerfile":
            specs.append(
                _GeneratedFileSpec(
                    path=_repo_relative_path(app.source_path, "Dockerfile"),
                    content=_render_dockerfile(app),
                )
            )
            continue
        if app.build_strategy == "docker_compose":
            specs.append(
                _GeneratedFileSpec(
                    path=_repo_relative_path(app.source_path, "docker-compose.yml"),
                    content=_render_docker_compose(app),
                )
            )
    return tuple(specs)


def _write_generated_files(*, repo_dir: Path, specs: Iterable[_GeneratedFileSpec]) -> tuple[str, ...]:
    generated: list[str] = []
    for spec in specs:
        file_path = repo_dir / spec.path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(spec.content, encoding="utf-8")
        generated.append(spec.path)
    return tuple(generated)


def _render_dockerfile(app: ProjectAppNormalizedCandidate) -> str:
    base_image = _base_image_for_app(app)
    start_command = _effective_start_command(app)
    port = app.exposed_port or _default_port_for_app(app)
    lines = [
        f"FROM {base_image}",
        "WORKDIR /app",
        "COPY . .",
    ]
    if port is not None:
        lines.append(f"EXPOSE {port}")
    lines.append(f'CMD ["sh", "-lc", {json.dumps(start_command)}]')
    return "\n".join(lines) + "\n"


def _render_docker_compose(app: ProjectAppNormalizedCandidate) -> str:
    base_image = _base_image_for_app(app)
    start_command = _effective_start_command(app)
    port = app.exposed_port or _default_port_for_app(app)
    lines = [
        "services:",
        "  app:",
        f"    image: {base_image}",
        "    working_dir: /app",
        "    volumes:",
        "      - .:/app",
        f'    command: ["sh", "-lc", {json.dumps(start_command)}]',
    ]
    if port is not None:
        lines.extend(
            [
                "    ports:",
                f'      - "{port}:{port}"',
            ]
        )
    return "\n".join(lines) + "\n"


def _effective_start_command(app: ProjectAppNormalizedCandidate) -> str:
    if app.start_command:
        return app.start_command
    runtime = _normalized_runtime(app)
    if runtime in {"node", "javascript", "typescript", "nextjs", "remix", "nuxt", "nestjs", "express", "astro", "sveltekit", "vite"}:
        return "npm run start"
    if runtime == "python":
        return "python app.py"
    if runtime == "go":
        return "go run ."
    if runtime == "rust":
        return "cargo run"
    if runtime == "ruby":
        port = app.exposed_port or 3000
        return f"bin/rails server -b 0.0.0.0 -p {port}"
    return "sh -lc 'echo \"Set a start command\" && exit 1'"


def _base_image_for_app(app: ProjectAppNormalizedCandidate) -> str:
    runtime = _normalized_runtime(app)
    if runtime in {"node", "javascript", "typescript", "nextjs", "remix", "nuxt", "nestjs", "express", "astro", "sveltekit", "vite"}:
        return "node:20-slim"
    if runtime == "python":
        return "python:3.12-slim"
    if runtime == "go":
        return "golang:1.22-alpine"
    if runtime == "rust":
        return "rust:1.78-slim"
    if runtime == "ruby":
        return "ruby:3.3-slim"
    return "python:3.12-slim"


def _default_port_for_app(app: ProjectAppNormalizedCandidate) -> int | None:
    runtime = _normalized_runtime(app)
    if runtime in {"node", "javascript", "typescript", "nextjs", "remix", "nuxt", "nestjs", "express", "astro", "sveltekit", "vite"}:
        return 3000
    if runtime == "python":
        return 8000
    if runtime == "go":
        return 8080
    if runtime == "rust":
        return 8080
    if runtime == "ruby":
        return 3000
    return app.exposed_port


def _normalized_runtime(app: ProjectAppNormalizedCandidate) -> str:
    return str(app.detected_runtime or app.detected_language or "").strip().lower()


def _build_pull_request_body(
    *,
    project: Project,
    analysis_run_id: str,
    generated_files: tuple[str, ...],
    base_branch: str,
    branch_name: str,
) -> str:
    files = "\n".join(f"- {path}" for path in generated_files)
    return (
        f"Automated deploy artifacts were generated for analysis run `{analysis_run_id}`.\n\n"
        f"Project: {project.project_id}\n"
        f"Base branch: `{base_branch}`\n"
        f"Head branch: `{branch_name}`\n\n"
        f"Generated files:\n{files}\n"
    )


def _repo_relative_path(source_path: str, filename: str) -> str:
    normalized_source = str(source_path or "").strip().replace("\\", "/")
    if normalized_source in {"", "."}:
        return filename
    return f"{normalized_source.rstrip('/')}/{filename}"


def _artifact_branch_name(*, analysis_run_id: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(analysis_run_id or "").strip().lower()).strip("-")
    return f"project-app-artifacts/{normalized or 'analysis'}"


def _repo_full_name(github_repository: str) -> str:
    normalized = normalize_repo_identifier(github_repository)
    prefix = "github.com/"
    if not normalized.startswith(prefix):
        raise RuntimeError("GitHub repository must point to github.com")
    repo_full_name = normalized[len(prefix) :].strip("/")
    if repo_full_name.count("/") != 1:
        raise RuntimeError("GitHub repository must be in owner/repo format")
    return repo_full_name


def _git_auth_env(token: str) -> dict[str, str]:
    credential = base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")
    return {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
    }


def _run_git(repo_dir: Path, args: list[str], *, token: str | None = None) -> str:
    env = _git_auth_env(token) if token else None
    process = subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if process.returncode != 0:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip() or f"git {' '.join(args)} failed")
    return process.stdout


def _resolve_remote_default_branch(repo_dir: Path, *, token: str) -> str:
    _run_git(repo_dir, ["fetch", "origin", "--prune"], token=token)
    try:
        symbolic_ref = _run_git(
            repo_dir,
            ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ).strip()
    except RuntimeError:
        symbolic_ref = ""
    if symbolic_ref.startswith("origin/"):
        branch_name = symbolic_ref[len("origin/") :].strip()
        if branch_name:
            return branch_name

    try:
        ls_remote_output = _run_git(repo_dir, ["ls-remote", "--symref", "origin", "HEAD"], token=token)
    except RuntimeError:
        ls_remote_output = ""
    for line in ls_remote_output.splitlines():
        if not line.startswith("ref: refs/heads/") or "\tHEAD" not in line:
            continue
        branch_name = line.removeprefix("ref: refs/heads/").split("\t", maxsplit=1)[0].strip()
        if branch_name:
            return branch_name

    return "main"


def _sync_local_base_branch_to_origin(repo_dir: Path, *, base_branch: str, token: str) -> None:
    normalized_base_branch = str(base_branch).strip()
    if not normalized_base_branch:
        raise RuntimeError("Base branch is required to generate project app artifacts")
    _run_git(repo_dir, ["fetch", "origin", normalized_base_branch], token=token)
    _run_git(repo_dir, ["checkout", "-B", normalized_base_branch, f"origin/{normalized_base_branch}"])


def _ensure_local_commit_identity(repo_dir: Path) -> None:
    user_name = _read_git_config(repo_dir, key="user.name")
    user_email = _read_git_config(repo_dir, key="user.email")
    if not user_name:
        _run_git(repo_dir, ["config", "user.name", "Master Builder Bot"])
    if not user_email:
        _run_git(repo_dir, ["config", "user.email", "master-builder-bot@users.noreply.github.com"])


def _read_git_config(repo_dir: Path, *, key: str) -> str | None:
    try:
        value = _run_git(repo_dir, ["config", "--get", key]).strip()
    except RuntimeError:
        return None
    return value or None
