from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.tools.git_ops import (
    GitWorkspaceManager,
    build_branch_name,
    enforce_repo_allowlist,
)


def _run(cmd: list[str], *, cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


class GitOpsTests(unittest.TestCase):
    def test_allowlist_normalizes_repo_urls(self) -> None:
        allowlist = ["https://github.com/example/repo"]
        enforce_repo_allowlist("git@github.com:Example/Repo.git", allowlist)

        with self.assertRaises(PermissionError):
            enforce_repo_allowlist("https://github.com/example/other-repo", allowlist)

    def test_build_branch_name_uses_expected_template(self) -> None:
        branch = build_branch_name("MAB-8", "GitHub App auth, git isolation, and PR creation")
        self.assertTrue(branch.startswith("jira/MAB-8-"))
        self.assertNotIn(" ", branch)

    def test_workspace_clone_branch_and_commit_flow(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_repo = root / "source-repo"
            source_repo.mkdir(parents=True, exist_ok=True)

            try:
                _run(["git", "init", "-b", "main"], cwd=source_repo)
            except subprocess.CalledProcessError:
                _run(["git", "init"], cwd=source_repo)
                _run(["git", "checkout", "-b", "main"], cwd=source_repo)
            _run(["git", "config", "user.email", "dev@example.test"], cwd=source_repo)
            _run(["git", "config", "user.name", "Dev Test"], cwd=source_repo)

            (source_repo / "README.md").write_text("seed\n", encoding="utf-8")
            _run(["git", "add", "README.md"], cwd=source_repo)
            _run(["git", "commit", "-m", "seed"], cwd=source_repo)

            manager = GitWorkspaceManager(base_dir=root / "workspaces")
            workspace = manager.prepare_workspace(
                tenant_id="tenant-a",
                issue_key="MAB-8",
                run_id="run-001",
            )
            manager.clone_repo(
                repo_url=str(source_repo),
                allowlist=[str(source_repo)],
                workspace=workspace,
            )

            _run(["git", "config", "user.email", "agent@example.test"], cwd=workspace.repo_dir)
            _run(["git", "config", "user.name", "Agent Test"], cwd=workspace.repo_dir)

            branch_name = manager.create_issue_branch(
                repo_dir=workspace.repo_dir,
                issue_key="MAB-8",
                summary="Add github app auth support",
            )
            self.assertTrue(branch_name.startswith("jira/MAB-8-"))

            readme = workspace.repo_dir / "README.md"
            readme.write_text("seed\nfeature\n", encoding="utf-8")
            sha = manager.commit_all(
                repo_dir=workspace.repo_dir,
                issue_key="MAB-8",
                summary="Add github app auth support",
            )
            self.assertTrue(sha.strip())

            message = subprocess.run(
                ["git", "log", "-1", "--pretty=%s"],
                cwd=str(workspace.repo_dir),
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(message, "MAB-8: Add github app auth support")

    def test_workspace_bootstrap_ci_copies_missing_workflows(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            target_repo = root / "target-repo"
            target_repo.mkdir(parents=True, exist_ok=True)
            (target_repo / "README.md").write_text("# Target\n", encoding="utf-8")

            manager = GitWorkspaceManager(base_dir=root / "workspaces")
            result = manager.bootstrap_ci_if_missing(repo_dir=target_repo)

            self.assertEqual(
                result.copied_workflows,
                (".github/workflows/ci.yml", ".github/workflows/security.yml"),
            )
            self.assertTrue(result.readme_updated)

    def test_push_branch_blocks_repo_not_in_allowlist(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            source_repo = root / "source-repo"
            source_repo.mkdir(parents=True, exist_ok=True)

            try:
                _run(["git", "init", "-b", "main"], cwd=source_repo)
            except subprocess.CalledProcessError:
                _run(["git", "init"], cwd=source_repo)
                _run(["git", "checkout", "-b", "main"], cwd=source_repo)
            _run(["git", "config", "user.email", "dev@example.test"], cwd=source_repo)
            _run(["git", "config", "user.name", "Dev Test"], cwd=source_repo)

            (source_repo / "README.md").write_text("seed\n", encoding="utf-8")
            _run(["git", "add", "README.md"], cwd=source_repo)
            _run(["git", "commit", "-m", "seed"], cwd=source_repo)

            manager = GitWorkspaceManager(base_dir=root / "workspaces")
            workspace = manager.prepare_workspace(
                tenant_id="tenant-a",
                issue_key="MAB-11",
                run_id="run-guardrail",
            )
            manager.clone_repo(
                repo_url=str(source_repo),
                allowlist=[str(source_repo)],
                workspace=workspace,
            )
            _run(["git", "config", "user.email", "agent@example.test"], cwd=workspace.repo_dir)
            _run(["git", "config", "user.name", "Agent Test"], cwd=workspace.repo_dir)
            branch_name = manager.create_issue_branch(
                repo_dir=workspace.repo_dir,
                issue_key="MAB-11",
                summary="Guardrail checks",
            )
            (workspace.repo_dir / "README.md").write_text("seed\nguardrails\n", encoding="utf-8")
            manager.commit_all(
                repo_dir=workspace.repo_dir,
                issue_key="MAB-11",
                summary="Guardrail checks",
            )

            with self.assertRaises(PermissionError):
                manager.push_branch(
                    repo_dir=workspace.repo_dir,
                    branch_name=branch_name,
                    allowlist=["https://github.com/example/other-repo"],
                )
