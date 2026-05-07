from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.storage.models import Project
from orchestrator.tools.repo_allowlist import normalize_repo_identifier


class ProjectRepoCheckoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedExecutionRepo:
    repo_dir: Path
    execution_branch: str
    start_point_ref: str
    start_point_sha: str
    workspace_key: str
    repo_kind: str
    issue_key: str | None = None


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

_MCP_SERVER_SECTION_PATTERN = re.compile(r"(?ms)^(\[mcp_servers\.(?P<name>[^\]]+)\]\s*\n)(.*?)(?=^\[|\Z)")
_FEATURES_SECTION_PATTERN = re.compile(r"(?ms)^(\[features\]\s*\n)(.*?)(?=^\[|\Z)")


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
    return redact_sensitive_text(
        re.sub(r"https://x-access-token:[^@]+@", "https://x-access-token:[REDACTED]@", message)
    )


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


def _resolve_git_path(*, repo_dir: Path, git_path: str) -> Path:
    resolved = _run_git(["rev-parse", "--git-path", git_path], cwd=repo_dir).strip()
    if not resolved:
        raise ProjectRepoCheckoutError(
            f"Unable to resolve git path '{git_path}' for repository bootstrap (repo_dir={repo_dir})"
        )
    candidate = Path(resolved)
    if not candidate.is_absolute():
        candidate = repo_dir / candidate
    return candidate


def project_repo_dir(*, base_dir: str, tenant_id: str, project_id: str) -> Path:
    return Path(base_dir) / tenant_id / project_id / "repo"


def project_checkout_root_dir(*, base_dir: str, tenant_id: str, project_id: str) -> Path:
    return Path(base_dir) / tenant_id / project_id


def project_run_root_dir(*, base_dir: str, tenant_id: str, project_id: str, run_id: str) -> Path:
    return Path(base_dir) / tenant_id / project_id / "runs" / run_id


def _normalize_workspace_key(value: object) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", str(value or "").strip().lower()).strip("-.")
    if not normalized:
        raise ProjectRepoCheckoutError("Worker workspace key must not be empty for run worktree setup")
    return normalized[:128]


def project_run_workspace_root_dir(
    *,
    base_dir: str,
    tenant_id: str,
    project_id: str,
    run_id: str,
    workspace_key: str,
) -> Path:
    return (
        project_run_root_dir(
            base_dir=base_dir,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
        )
        / "workspaces"
        / _normalize_workspace_key(workspace_key)
    )


def project_run_repo_dir(
    *,
    base_dir: str,
    tenant_id: str,
    project_id: str,
    run_id: str,
    workspace_key: str,
) -> Path:
    return project_run_workspace_root_dir(
        base_dir=base_dir,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
        workspace_key=workspace_key,
    ) / "repo"


def execution_branch_name(*, issue_key: str, run_id: str) -> str:
    normalized_issue = re.sub(r"[^a-z0-9._/-]+", "-", str(issue_key).strip().lower()).strip("-")
    normalized_run = re.sub(r"[^a-z0-9._/-]+", "-", str(run_id).strip().lower()).strip("-")
    issue_component = normalized_issue or "issue"
    run_component = normalized_run or "run"
    return f"run/{issue_component}/{run_component}"


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
        _restrict_external_tool_surfaces_in_project_codex(repo_dir=repo_dir)

    gitignore_path = repo_dir / ".gitignore"
    seeded_gitignore = False
    if not gitignore_path.exists():
        gitignore_path.write_text(_build_seeded_gitignore_content(repo_dir=repo_dir), encoding="utf-8")
        seeded_gitignore = True

    # Keep workspace policy files out of accidental commits inside project repos/worktrees.
    exclude_path = _resolve_git_path(repo_dir=repo_dir, git_path="info/exclude")
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = set()
    if exclude_path.exists():
        existing_lines = {
            line.strip()
            for line in exclude_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    required_lines = {"AGENTS.md", ".codex/", ".master-builder-run.json", ".master-builder-execution-repo.json"}
    if seeded_gitignore:
        required_lines.add(".gitignore")
    missing_lines = [line for line in sorted(required_lines) if line not in existing_lines]
    if missing_lines:
        prefix = "\n" if exclude_path.exists() and exclude_path.read_text(encoding="utf-8") else ""
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(missing_lines) + "\n")


def _run_metadata_path(*, repo_dir: Path | str) -> Path:
    return Path(repo_dir) / ".master-builder-run.json"


def _execution_repo_metadata_path(*, repo_dir: Path | str) -> Path:
    return Path(repo_dir) / ".master-builder-execution-repo.json"


def read_run_worktree_metadata(*, repo_dir: Path | str) -> dict[str, str] | None:
    return _read_run_metadata(repo_dir=repo_dir)


def _write_run_metadata(
    *,
    repo_dir: Path,
    run_id: str,
    issue_key: str,
    execution_branch: str,
    base_branch: str,
    integration_branch: str,
    workspace_key: str,
    start_point_ref: str,
    start_point_sha: str,
) -> None:
    _run_metadata_path(repo_dir=repo_dir).write_text(
        json.dumps(
            {
                "run_id": run_id,
                "issue_key": issue_key,
                "execution_branch": execution_branch,
                "base_branch": base_branch,
                "integration_branch": integration_branch,
                "workspace_key": workspace_key,
                "start_point_ref": start_point_ref,
                "start_point_sha": start_point_sha,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _read_run_metadata(*, repo_dir: Path | str) -> dict[str, str] | None:
    path = _run_metadata_path(repo_dir=repo_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def read_execution_repo_metadata(*, repo_dir: Path | str) -> dict[str, str] | None:
    path = _execution_repo_metadata_path(repo_dir=repo_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def validate_execution_repo(
    *,
    checkout_root: Path,
    repo_dir: Path | str,
    run_id: str,
    execution_branch: str,
    workspace_key: str,
) -> PreparedExecutionRepo:
    resolved_checkout_root = checkout_root.resolve()
    resolved_repo_dir = Path(repo_dir).resolve()
    try:
        resolved_repo_dir.relative_to(resolved_checkout_root)
    except ValueError as exc:
        raise ProjectRepoCheckoutError(
            f"Execution repo {resolved_repo_dir} is outside checkout root {resolved_checkout_root}"
        ) from exc
    if not (resolved_repo_dir / ".git").exists():
        raise ProjectRepoCheckoutError(f"Execution repo is missing git metadata at {resolved_repo_dir}")

    _sync_agent_workspace_files(repo_dir=resolved_repo_dir)
    metadata = read_execution_repo_metadata(repo_dir=resolved_repo_dir)
    if metadata is None:
        raise ProjectRepoCheckoutError(f"Execution repo metadata is missing at {resolved_repo_dir}")
    if str(metadata.get("run_id") or "").strip() != str(run_id or "").strip():
        raise ProjectRepoCheckoutError(f"Execution repo metadata does not match run_id={run_id}")

    normalized_workspace_key = _normalize_workspace_key(workspace_key)
    actual_workspace_key = str(metadata.get("workspace_key") or "").strip()
    if actual_workspace_key != normalized_workspace_key:
        raise ProjectRepoCheckoutError(
            "Execution repo metadata workspace_key mismatch: "
            f"expected {normalized_workspace_key}, found {actual_workspace_key or '<missing>'}"
        )

    expected_branch = str(metadata.get("execution_branch") or "").strip() or str(execution_branch or "").strip()
    if not expected_branch:
        raise ProjectRepoCheckoutError("Execution repo metadata is missing execution_branch")
    current_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=resolved_repo_dir).strip()
    if current_branch != expected_branch:
        raise ProjectRepoCheckoutError(
            f"Execution repo branch mismatch: expected {expected_branch}, found {current_branch}"
        )
    if _run_git(["status", "--porcelain"], cwd=resolved_repo_dir).strip():
        raise ProjectRepoCheckoutError("Execution repo is unexpectedly dirty before execution")

    start_point_ref = str(metadata.get("start_point_ref") or "").strip()
    start_point_sha = str(metadata.get("start_point_sha") or "").strip()
    repo_kind = str(metadata.get("repo_kind") or "").strip()
    if not start_point_ref or not start_point_sha:
        raise ProjectRepoCheckoutError("Execution repo metadata is missing start point data")
    if not repo_kind:
        raise ProjectRepoCheckoutError("Execution repo metadata is missing repo_kind")

    issue_key = str(metadata.get("issue_key") or "").strip() or None
    return PreparedExecutionRepo(
        repo_dir=resolved_repo_dir,
        execution_branch=expected_branch,
        start_point_ref=start_point_ref,
        start_point_sha=start_point_sha,
        workspace_key=normalized_workspace_key,
        repo_kind=repo_kind,
        issue_key=issue_key,
    )


def _git_ref_exists(*, cwd: Path, ref: str) -> bool:
    process = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", ref],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    return process.returncode == 0


def _resolve_worktree_start_point(*, repo_dir: Path, base_branch: str, integration_branch: str) -> str:
    candidate_refs = [
        f"refs/remotes/origin/{integration_branch}",
        f"refs/heads/{integration_branch}",
        f"refs/remotes/origin/{base_branch}",
        f"refs/heads/{base_branch}",
        "HEAD",
    ]
    for ref in candidate_refs:
        if ref == "HEAD" or _git_ref_exists(cwd=repo_dir, ref=ref):
            if ref.startswith("refs/remotes/origin/"):
                return f"origin/{ref.removeprefix('refs/remotes/origin/')}"
            if ref.startswith("refs/heads/"):
                return ref.removeprefix("refs/heads/")
            return ref
    return "HEAD"


def _resolve_ref_commit_sha(*, repo_dir: Path, ref: str) -> str:
    return _run_git(["rev-parse", ref], cwd=repo_dir).strip()


def _remove_run_worktree(*, repo_dir: Path, run_repo_dir: Path) -> None:
    run_root = run_repo_dir.parent
    if (run_repo_dir / ".git").exists():
        try:
            _run_git(["worktree", "remove", "--force", str(run_repo_dir)], cwd=repo_dir)
        except ProjectRepoCheckoutError:
            shutil.rmtree(run_root, ignore_errors=True)
            return
    if run_root.exists():
        shutil.rmtree(run_root, ignore_errors=True)


def _is_worktree_checkout_usable(*, run_repo_dir: Path) -> bool:
    if not (run_repo_dir / ".git").exists():
        return False
    process = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=str(run_repo_dir),
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0:
        return False
    return process.stdout.strip().lower() == "true"


def cleanup_run_workspaces(
    *,
    base_dir: str,
    tenant_id: str,
    project_id: str,
    run_id: str,
    workspace_key: str | None = None,
) -> None:
    repo_dir = project_repo_dir(base_dir=base_dir, tenant_id=tenant_id, project_id=project_id)
    run_root = project_run_root_dir(base_dir=base_dir, tenant_id=tenant_id, project_id=project_id, run_id=run_id)
    workspaces_root = run_root / "workspaces"
    if workspace_key is not None:
        workspace_root = project_run_workspace_root_dir(
            base_dir=base_dir,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            workspace_key=workspace_key,
        )
        if workspace_root.exists():
            shutil.rmtree(workspace_root, ignore_errors=True)
        if workspaces_root.exists():
            try:
                workspaces_root.rmdir()
            except OSError:
                pass
    else:
        if workspaces_root.exists():
            shutil.rmtree(workspaces_root, ignore_errors=True)

    if run_root.exists():
        try:
            run_root.rmdir()
        except OSError:
            pass
    if not (repo_dir / ".git").exists():
        return
    try:
        _run_git(["worktree", "prune"], cwd=repo_dir)
    except ProjectRepoCheckoutError:
        return


@dataclass(frozen=True)
class RunSnapshotFreshness:
    start_point_ref: str
    start_point_sha: str
    current_start_point_sha: str | None
    stale: bool
    message: str | None


def check_run_snapshot_freshness(
    *,
    base_dir: str,
    tenant_id: str,
    project: Project,
    start_point_ref: str,
    start_point_sha: str,
) -> RunSnapshotFreshness:
    repo_dir = project_repo_dir(base_dir=base_dir, tenant_id=tenant_id, project_id=project.project_id)
    _run_git(["fetch", "origin", "--prune"], cwd=repo_dir)
    try:
        current_sha = _resolve_ref_commit_sha(repo_dir=repo_dir, ref=start_point_ref)
    except ProjectRepoCheckoutError:
        return RunSnapshotFreshness(
            start_point_ref=start_point_ref,
            start_point_sha=start_point_sha,
            current_start_point_sha=None,
            stale=True,
            message=(
                f"Branch snapshot stale: start ref {start_point_ref} is no longer available upstream. "
                "Requeueing from the latest snapshot."
            ),
        )

    stale = current_sha != start_point_sha
    message = None
    if stale:
        message = (
            f"Branch snapshot stale: {start_point_ref} moved from {start_point_sha[:12]} "
            f"to {current_sha[:12]}. Requeueing from the latest snapshot."
        )
    return RunSnapshotFreshness(
        start_point_ref=start_point_ref,
        start_point_sha=start_point_sha,
        current_start_point_sha=current_sha,
        stale=stale,
        message=message,
    )


def ensure_run_worktree(
    *,
    base_dir: str,
    tenant_id: str,
    project: Project,
    run_id: str,
    issue_key: str,
    base_branch: str,
    integration_branch: str,
    workspace_key: str,
) -> tuple[Path, str]:
    repo_dir = project_repo_dir(base_dir=base_dir, tenant_id=tenant_id, project_id=project.project_id)
    if not (repo_dir / ".git").exists():
        raise ProjectRepoCheckoutError(
            f"Project repository checkout is missing for run worktree creation (repo_dir={repo_dir})"
        )
    normalized_workspace_key = _normalize_workspace_key(workspace_key)

    run_repo = project_run_repo_dir(
        base_dir=base_dir,
        tenant_id=tenant_id,
        project_id=project.project_id,
        run_id=run_id,
        workspace_key=normalized_workspace_key,
    )
    execution_branch = execution_branch_name(issue_key=issue_key, run_id=run_id)
    _run_git(["fetch", "origin", "--prune"], cwd=repo_dir)
    start_point_ref = _resolve_worktree_start_point(
        repo_dir=repo_dir,
        base_branch=base_branch,
        integration_branch=integration_branch,
    )
    start_point_sha = _resolve_ref_commit_sha(repo_dir=repo_dir, ref=start_point_ref)
    if (run_repo / ".git").exists():
        if not _is_worktree_checkout_usable(run_repo_dir=run_repo):
            _remove_run_worktree(repo_dir=repo_dir, run_repo_dir=run_repo)
            _run_git(["worktree", "prune"], cwd=repo_dir)
        else:
            metadata = _read_run_metadata(repo_dir=run_repo)
            if metadata is not None and (
                str(metadata.get("run_id") or "") == run_id
                and str(metadata.get("execution_branch") or "") == execution_branch
                and str(metadata.get("base_branch") or "") == base_branch
                and str(metadata.get("integration_branch") or "") == integration_branch
                and str(metadata.get("workspace_key") or "") == normalized_workspace_key
                and str(metadata.get("start_point_ref") or "") == start_point_ref
                and str(metadata.get("start_point_sha") or "") == start_point_sha
            ):
                _sync_agent_workspace_files(repo_dir=run_repo)
                return run_repo, execution_branch
            _remove_run_worktree(repo_dir=repo_dir, run_repo_dir=run_repo)
            _run_git(["worktree", "prune"], cwd=repo_dir)
    elif run_repo.exists():
        shutil.rmtree(run_repo.parent, ignore_errors=True)

    run_repo.parent.mkdir(parents=True, exist_ok=True)
    _run_git(
        ["worktree", "add", "--force", "-B", execution_branch, str(run_repo), start_point_sha],
        cwd=repo_dir,
    )
    _sync_agent_workspace_files(repo_dir=run_repo)
    _write_run_metadata(
        repo_dir=run_repo,
        run_id=run_id,
        issue_key=issue_key,
        execution_branch=execution_branch,
        base_branch=base_branch,
        integration_branch=integration_branch,
        workspace_key=normalized_workspace_key,
        start_point_ref=start_point_ref,
        start_point_sha=start_point_sha,
    )
    return run_repo, execution_branch


def validate_run_worktree(
    *,
    repo_dir: Path,
    run_id: str,
    execution_branch: str,
    workspace_key: str | None = None,
) -> str | None:
    if not (repo_dir / ".git").exists():
        return "run worktree is missing its git metadata"
    metadata = _read_run_metadata(repo_dir=repo_dir)
    if metadata is None:
        return "run worktree metadata is missing"
    if str(metadata.get("run_id") or "") != run_id:
        return f"run worktree metadata does not match run_id={run_id}"
    if workspace_key is not None:
        normalized_workspace_key = _normalize_workspace_key(workspace_key)
        actual_workspace_key = str(metadata.get("workspace_key") or "").strip()
        if actual_workspace_key != normalized_workspace_key:
            return (
                "run worktree metadata workspace_key mismatch: "
                f"expected {normalized_workspace_key}, found {actual_workspace_key or '<missing>'}"
            )
    expected_branch = str(metadata.get("execution_branch") or "").strip() or execution_branch
    current_branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_dir).strip()
    if current_branch != expected_branch:
        return f"run worktree branch mismatch: expected {expected_branch}, found {current_branch}"
    if _run_git(["status", "--porcelain"], cwd=repo_dir).strip():
        return "run worktree is unexpectedly dirty before execution"
    return None


def _restrict_external_tool_surfaces_in_project_codex(*, repo_dir: Path) -> None:
    config_path = repo_dir / ".codex" / "config.toml"
    if not config_path.exists():
        return
    raw = config_path.read_text(encoding="utf-8")
    updated = raw
    cursor = 0
    while True:
        section_match = _MCP_SERVER_SECTION_PATTERN.search(updated, cursor)
        if section_match is None:
            break
        section_header = section_match.group(1)
        section_body = section_match.group(3)
        if re.search(r"(?m)^\s*enabled\s*=\s*(true|false)\s*$", section_body):
            section_body = re.sub(
                r"(?m)^\s*enabled\s*=\s*(true|false)\s*$",
                "enabled = false",
                section_body,
                count=1,
            )
        else:
            if not section_body.endswith("\n"):
                section_body += "\n"
            section_body += "enabled = false\n"
        # Keep TOML sections separated even when source file omits trailing newline.
        if not section_body.endswith("\n"):
            section_body += "\n"
        updated = (
            f"{updated[:section_match.start()]}"
            f"{section_header}{section_body}"
            f"{updated[section_match.end():]}"
        )
        cursor = section_match.start() + len(section_header) + len(section_body)

    features_match = _FEATURES_SECTION_PATTERN.search(updated)
    if features_match is None:
        suffix = "" if updated.endswith("\n") else "\n"
        updated = (
            f"{updated}{suffix}\n[features]\n"
            "apps = false\n"
            "plugins = false\n"
        )
    else:
        section_header = features_match.group(1)
        section_body = features_match.group(2)
        for feature_name in ("apps", "plugins"):
            feature_pattern = rf"(?m)^\s*{re.escape(feature_name)}\s*=\s*(true|false)\s*$"
            if re.search(feature_pattern, section_body):
                section_body = re.sub(
                    feature_pattern,
                    f"{feature_name} = false",
                    section_body,
                    count=1,
                )
            else:
                if not section_body.endswith("\n"):
                    section_body += "\n"
                section_body += f"{feature_name} = false\n"
        if not section_body.endswith("\n"):
            section_body += "\n"
        updated = (
            f"{updated[:features_match.start()]}"
            f"{section_header}{section_body}"
            f"{updated[features_match.end():]}"
        )
    if updated != raw:
        config_path.write_text(updated, encoding="utf-8")


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
