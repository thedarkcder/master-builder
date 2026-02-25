from __future__ import annotations

import base64
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


_SEEDED_GITIGNORE_CONTENT = """# Seeded by Master Builder
# Add project-specific ignore rules below.
.DS_Store
Thumbs.db
*.log
.env
.env.*
!.env.example
"""

_SEEDED_GITIGNORE_SWIFT_CONTENT = """

# Swift / Xcode
DerivedData/
*.xcuserstate
*.xcworkspace/xcuserdata/
*.xcodeproj/project.xcworkspace/xcuserdata/
"""

_SEEDED_GITIGNORE_NEXT_CONTENT = """

# Next.js / Node
node_modules/
.next/
out/
npm-debug.log*
yarn-debug.log*
yarn-error.log*
pnpm-debug.log*
"""

_SEEDED_GITIGNORE_JAVA_CONTENT = """

# Java
target/
build/
out/
*.class
"""


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


def _github_git_extraheader(github_installation_token: str) -> str:
    credential_bytes = f"x-access-token:{github_installation_token}".encode("utf-8")
    credential = base64.b64encode(credential_bytes).decode("ascii")
    return f"AUTHORIZATION: basic {credential}"


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


def _is_swift_repo(repo_dir: Path) -> bool:
    if (repo_dir / "Package.swift").exists():
        return True
    if any(repo_dir.glob("*.xcodeproj")):
        return True
    if any(repo_dir.glob("*.xcworkspace")):
        return True
    return False


def _is_next_repo(repo_dir: Path) -> bool:
    if any(
        (repo_dir / filename).exists()
        for filename in ("next.config.js", "next.config.mjs", "next.config.ts", "next.config.cjs")
    ):
        return True
    if (repo_dir / "next-env.d.ts").exists():
        return True
    return False


def _is_java_repo(repo_dir: Path) -> bool:
    if any((repo_dir / filename).exists() for filename in ("pom.xml", "build.gradle", "build.gradle.kts", "gradlew")):
        return True
    return False


def _build_seeded_gitignore_content(*, repo_dir: Path) -> str:
    sections: list[str] = [_SEEDED_GITIGNORE_CONTENT]
    if _is_swift_repo(repo_dir):
        sections.append(_SEEDED_GITIGNORE_SWIFT_CONTENT)
    if _is_next_repo(repo_dir):
        sections.append(_SEEDED_GITIGNORE_NEXT_CONTENT)
    if _is_java_repo(repo_dir):
        sections.append(_SEEDED_GITIGNORE_JAVA_CONTENT)
    return "".join(sections)


def _sync_agent_workspace_files(*, repo_dir: Path) -> None:
    source_root = Path(__file__).resolve().parents[2]
    agents_src = source_root / "AGENTS.md"
    codex_src = source_root / ".codex"

    if agents_src.exists():
        shutil.copy2(agents_src, repo_dir / "AGENTS.md")

    if codex_src.exists() and codex_src.is_dir():
        shutil.copytree(codex_src, repo_dir / ".codex", dirs_exist_ok=True)

    gitignore_path = repo_dir / ".gitignore"
    if not gitignore_path.exists():
        gitignore_path.write_text(_build_seeded_gitignore_content(repo_dir=repo_dir), encoding="utf-8")

    # Keep workspace policy files out of accidental commits inside project repos.
    info_dir = repo_dir / ".git" / "info"
    info_dir.mkdir(parents=True, exist_ok=True)
    exclude_path = info_dir / "exclude"
    existing_lines = set()
    if exclude_path.exists():
        existing_lines = {
            line.strip()
            for line in exclude_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    required_lines = {"AGENTS.md", ".codex/"}
    missing_lines = [line for line in sorted(required_lines) if line not in existing_lines]
    if missing_lines:
        prefix = "\n" if exclude_path.exists() and exclude_path.read_text(encoding="utf-8") else ""
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(missing_lines) + "\n")


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
        _sync_agent_workspace_files(repo_dir=repo_dir)
        return repo_dir

    repo_full_name = _repo_full_name(project.github_repository)
    clone_url = f"https://github.com/{repo_full_name}.git"
    clone_env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": _github_git_extraheader(github_installation_token),
    }
    try:
        _run_git(["clone", "--origin", "origin", clone_url, str(repo_dir)], cwd=repo_root, env=clone_env)
    except ProjectRepoCheckoutError:
        if repo_dir.exists():
            shutil.rmtree(repo_dir, ignore_errors=True)
        raise
    _run_git(["remote", "set-url", "origin", project.github_repository], cwd=repo_dir)
    _sync_agent_workspace_files(repo_dir=repo_dir)
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
