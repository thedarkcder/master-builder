from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from orchestrator.tools.bootstrap import WorkflowBootstrapResult, bootstrap_ci_workflows
from orchestrator.tools.repo_allowlist import enforce_repo_allowlist as _enforce_repo_allowlist


class GitOperationError(RuntimeError):
    pass


def _slugify(text: str, *, max_length: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower()).strip("-")
    if not cleaned:
        return "update"
    if len(cleaned) <= max_length:
        return cleaned
    return cleaned[:max_length].rstrip("-")


def enforce_repo_allowlist(repo_url: str, allowlist: list[str]) -> None:
    _enforce_repo_allowlist(repo_url, allowlist)


def build_branch_name(issue_key: str, summary: str) -> str:
    return f"jira/{issue_key}-{_slugify(summary)}"


@dataclass(frozen=True)
class WorkspacePaths:
    workspace_dir: Path
    repo_dir: Path


class GitWorkspaceManager:
    def __init__(self, base_dir: Path | str = "/tmp/master-builder-workspaces"):
        self._base_dir = Path(base_dir)

    def prepare_workspace(self, *, tenant_id: str, issue_key: str, run_id: str) -> WorkspacePaths:
        workspace = self._base_dir / tenant_id / issue_key / run_id
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True, exist_ok=True)
        return WorkspacePaths(workspace_dir=workspace, repo_dir=workspace / "repo")

    def clone_repo(self, *, repo_url: str, allowlist: list[str], workspace: WorkspacePaths) -> None:
        enforce_repo_allowlist(repo_url, allowlist)
        self._run_git(
            ["clone", "--depth", "1", repo_url, str(workspace.repo_dir)],
            cwd=workspace.workspace_dir,
        )

    def create_issue_branch(self, *, repo_dir: Path, issue_key: str, summary: str) -> str:
        branch_name = build_branch_name(issue_key, summary)
        self._run_git(["checkout", "-b", branch_name], cwd=repo_dir)
        return branch_name

    def commit_all(self, *, repo_dir: Path, issue_key: str, summary: str) -> str:
        self._run_git(["add", "-A"], cwd=repo_dir)
        self._run_git(["commit", "-m", f"{issue_key}: {summary}"], cwd=repo_dir)
        sha = self._run_git(["rev-parse", "HEAD"], cwd=repo_dir).strip()
        return sha

    def push_branch(self, *, repo_dir: Path, branch_name: str, allowlist: list[str]) -> None:
        remote_url = self._run_git(["config", "--get", "remote.origin.url"], cwd=repo_dir).strip()
        if not remote_url:
            raise GitOperationError("Git remote.origin.url is missing for workspace")
        _enforce_repo_allowlist(remote_url, allowlist)
        self._run_git(["push", "-u", "origin", branch_name], cwd=repo_dir)

    def bootstrap_ci_if_missing(self, *, repo_dir: Path, template_repo_root: Path | None = None) -> WorkflowBootstrapResult:
        return bootstrap_ci_workflows(
            target_repo_dir=repo_dir,
            template_repo_root=template_repo_root,
        )

    def _run_git(self, args: list[str], *, cwd: Path) -> str:
        process = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            raise GitOperationError(
                f"git {' '.join(args)} failed: {process.stderr.strip() or process.stdout.strip()}"
            )
        return process.stdout
