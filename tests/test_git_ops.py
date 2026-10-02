from __future__ import annotations

import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant
from orchestrator.tools.git_ops import (
    GitWorkspaceManager,
    build_branch_name,
    enforce_repo_match,
)


def _run(cmd: list[str], *, cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


class GitOpsTests(unittest.TestCase):
    def test_repo_match_normalizes_repo_urls(self) -> None:
        github_repository = "https://github.com/example/repo"
        enforce_repo_match("git@github.com:Example/Repo.git", github_repository)

        with self.assertRaises(PermissionError):
            enforce_repo_match(
                "https://github.com/example/other-repo", github_repository
            )

    def test_build_branch_name_uses_expected_template(self) -> None:
        branch = build_branch_name(
            "MAB-8", "GitHub App auth, git isolation, and PR creation"
        )
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
                github_repository=str(source_repo),
                workspace=workspace,
            )

            _run(
                ["git", "config", "user.email", "agent@example.test"],
                cwd=workspace.repo_dir,
            )
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

    def test_workspace_bootstrap_codex_uses_persisted_state(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            database_url = f"sqlite:///{root}/git_ops_bootstrap.db"
            run_migrations(database_url=database_url)
            reset_db_engine_cache()
            session_factory = create_session_factory(database_url=database_url)

            with session_factory() as session:
                now = datetime.now(timezone.utc)
                session.add(
                    Tenant(
                        tenant_id="tenant-a",
                        name="Tenant A",
                        is_enabled=True,
                        jira_config={
                            "mcp_endpoint": "https://mcp.example.test",
                            "auth_ref": "secret/jira",
                            "project_keys": ["TP"],
                            "ready_label": "agent:ready",
                            "in_progress_label": "agent:in-progress",
                            "blocked_label": "agent:blocked",
                            "done_label": "agent:done",
                            "ready_jql": "project = TP",
                            "webhook_secret_ref": None,
                        },
                        github_config={
                            "mode": "github_app",
                            "app_id_ref": "secret/app-id",
                            "private_key_ref": "secret/private-key",
                            "webhook_secret_ref": None,
                            "installation_id": "12345",
                        },
                        repos_config={
                            "github_repository": "https://github.com/example/repo",
                        },
                        policy_config={
                            "allow_jira_transitions": False,
                            "allow_pr_creation": True,
                            "allow_label_mutations": True,
                            "max_runtime_minutes": 30,
                            "max_dev_test_review_loops": 2,
                            "max_concurrent_runs": 2,
                            "allowed_commands": [],
                            "require_agents_md": False,
                        },
                        discord_config=None,
                        created_at=now,
                        updated_at=now,
                    )
                )
                session.commit()

            target_repo = root / "target-repo"
            target_repo.mkdir(parents=True, exist_ok=True)

            manager = GitWorkspaceManager(base_dir=root / "workspaces")
            with session_factory() as session:
                first = manager.bootstrap_codex_if_missing(
                    session=session,
                    tenant_id="tenant-a",
                    repo_url="https://github.com/example/repo",
                    repo_dir=target_repo,
                    run_id="run-001",
                    branch_name="jira/MAB-14-bootstrap",
                )
                second = manager.bootstrap_codex_if_missing(
                    session=session,
                    tenant_id="tenant-a",
                    repo_url="https://github.com/example/repo",
                    repo_dir=target_repo,
                    run_id="run-002",
                    branch_name="jira/MAB-14-followup",
                )

            self.assertTrue(first.created_files)
            self.assertFalse(second.created_files)

            context = manager.load_preflight_codex_context(repo_dir=target_repo)
            self.assertIn("Never add or expose secrets", context["policy"])

    def test_push_branch_blocks_repo_not_matching_tenant_repository(self) -> None:
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
                github_repository=str(source_repo),
                workspace=workspace,
            )
            _run(
                ["git", "config", "user.email", "agent@example.test"],
                cwd=workspace.repo_dir,
            )
            _run(["git", "config", "user.name", "Agent Test"], cwd=workspace.repo_dir)
            branch_name = manager.create_issue_branch(
                repo_dir=workspace.repo_dir,
                issue_key="MAB-11",
                summary="Guardrail checks",
            )
            (workspace.repo_dir / "README.md").write_text(
                "seed\nguardrails\n", encoding="utf-8"
            )
            manager.commit_all(
                repo_dir=workspace.repo_dir,
                issue_key="MAB-11",
                summary="Guardrail checks",
            )

            with self.assertRaises(PermissionError):
                manager.push_branch(
                    repo_dir=workspace.repo_dir,
                    branch_name=branch_name,
                    github_repository="https://github.com/example/other-repo",
                )
