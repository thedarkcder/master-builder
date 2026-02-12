from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.storage.models import Project
from orchestrator.tools.repo_allowlist import normalize_repo_identifier


class ProjectRepoCheckoutError(RuntimeError):
    pass


def _repo_full_name(repository_url: str) -> str:
    normalized = normalize_repo_identifier(repository_url)
    prefix = "github.com/"
    if not normalized.startswith(prefix):
        raise ProjectRepoCheckoutError("Only GitHub repositories are supported for project checkout")
    repo_full_name = normalized[len(prefix):].strip("/")
    if repo_full_name.count("/") != 1:
        raise ProjectRepoCheckoutError("Repository URL must be in owner/repo format")
    return repo_full_name


def _safe_git_error(stderr: str, stdout: str) -> str:
    message = (stderr or stdout or "git command failed").strip()
    return re.sub(r"https://x-access-token:[^@]+@", "https://x-access-token:[REDACTED]@", message)


def _run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if process.returncode != 0:
        raise ProjectRepoCheckoutError(_safe_git_error(process.stderr, process.stdout))
    return process.stdout


def project_repo_dir(*, base_dir: str, tenant_id: str, project_id: str) -> Path:
    return Path(base_dir) / tenant_id / project_id / "repo"


def ensure_project_checkout(
    *,
    base_dir: str,
    tenant_id: str,
    project: Project,
    github_installation_token: str,
) -> Path:
    repo_dir = project_repo_dir(base_dir=base_dir, tenant_id=tenant_id, project_id=project.project_id)
    repo_root = repo_dir.parent
    repo_root.mkdir(parents=True, exist_ok=True)
    git_dir = repo_dir / ".git"
    if git_dir.exists():
        return repo_dir

    repo_full_name = _repo_full_name(project.github_repository)
    clone_url = f"https://github.com/{repo_full_name}.git"
    clone_env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {github_installation_token}",
    }
    try:
        _run_git(["clone", "--origin", "origin", clone_url, str(repo_dir)], cwd=repo_root, env=clone_env)
    except ProjectRepoCheckoutError:
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        raise
    _run_git(["remote", "set-url", "origin", project.github_repository], cwd=repo_dir)
    return repo_dir


@dataclass(frozen=True)
class LocalRepoContext:
    available: bool
    reason: str | None
    repo_dir: str | None
    current_branch: str | None
    head_sha: str | None
    branches: list[str]
    recent_commits: list[str]
    issue_related_commits: list[str]


def collect_local_repo_context(
    *,
    base_dir: str,
    tenant_id: str,
    project: Project,
    issue_key: str | None = None,
) -> LocalRepoContext:
    repo_dir = project_repo_dir(base_dir=base_dir, tenant_id=tenant_id, project_id=project.project_id)
    if not (repo_dir / ".git").exists():
        return LocalRepoContext(
            available=False,
            reason="repository_not_cloned",
            repo_dir=str(repo_dir),
            current_branch=None,
            head_sha=None,
            branches=[],
            recent_commits=[],
            issue_related_commits=[],
        )

    try:
        current_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir).strip() or None
        head_sha = _run_git(["rev-parse", "HEAD"], cwd=repo_dir).strip() or None
        branches_raw = _run_git(
            [
                "for-each-ref",
                "--format=%(refname:short)",
                "--sort=-committerdate",
                "refs/heads",
                "refs/remotes/origin",
            ],
            cwd=repo_dir,
        )
        recent_commits_raw = _run_git(["log", "--oneline", "--decorate", "-n", "25", "--all"], cwd=repo_dir)
        issue_commit_raw = ""
        normalized_issue_key = str(issue_key or "").strip().upper()
        if normalized_issue_key:
            issue_commit_raw = _run_git(
                ["log", "--oneline", "--decorate", "--all", "--grep", normalized_issue_key, "-n", "10"],
                cwd=repo_dir,
            )
    except ProjectRepoCheckoutError as exc:
        return LocalRepoContext(
            available=False,
            reason=f"git_context_unavailable:{exc}",
            repo_dir=str(repo_dir),
            current_branch=None,
            head_sha=None,
            branches=[],
            recent_commits=[],
            issue_related_commits=[],
        )

    branches = [line.strip() for line in branches_raw.splitlines() if line.strip()][:20]
    recent_commits = [line.strip() for line in recent_commits_raw.splitlines() if line.strip()][:25]
    issue_related_commits = [line.strip() for line in issue_commit_raw.splitlines() if line.strip()][:10]
    return LocalRepoContext(
        available=True,
        reason=None,
        repo_dir=str(repo_dir),
        current_branch=current_branch,
        head_sha=head_sha,
        branches=branches,
        recent_commits=recent_commits,
        issue_related_commits=issue_related_commits,
    )
