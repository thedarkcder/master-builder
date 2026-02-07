from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant
from orchestrator.tools.bootstrap import (
    bootstrap_ci_workflows,
    bootstrap_codex_assets,
    ensure_codex_bootstrap_state,
    has_required_codex_assets,
    load_codex_preflight_context,
    list_repo_bootstrap_states,
)


class WorkflowBootstrapTests(unittest.TestCase):
    def test_bootstrap_copies_missing_workflows_and_updates_readme(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            target_repo = Path(tmp_dir) / "target-repo"
            target_repo.mkdir(parents=True, exist_ok=True)
            (target_repo / "README.md").write_text("# Target Repo\n", encoding="utf-8")

            result = bootstrap_ci_workflows(target_repo_dir=target_repo)

            self.assertEqual(
                result.copied_workflows,
                (".github/workflows/ci.yml", ".github/workflows/security.yml"),
            )
            self.assertTrue(result.readme_updated)
            self.assertTrue((target_repo / ".github" / "workflows" / "ci.yml").exists())
            self.assertTrue((target_repo / ".github" / "workflows" / "security.yml").exists())
            readme = (target_repo / "README.md").read_text(encoding="utf-8")
            self.assertIn("## CI and Security Checks", readme)

    def test_bootstrap_is_idempotent_and_non_destructive(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            target_repo = Path(tmp_dir) / "target-repo"
            target_repo.mkdir(parents=True, exist_ok=True)
            (target_repo / "README.md").write_text("# Target Repo\n", encoding="utf-8")

            first = bootstrap_ci_workflows(target_repo_dir=target_repo)
            second = bootstrap_ci_workflows(target_repo_dir=target_repo)

            self.assertTrue(first.copied_workflows)
            self.assertFalse(second.copied_workflows)
            self.assertFalse(second.readme_updated)


class CodexBootstrapTests(unittest.TestCase):
    def test_codex_bootstrap_creates_required_assets_and_preflight_context(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            target_repo = Path(tmp_dir) / "target-repo"
            target_repo.mkdir(parents=True, exist_ok=True)

            result = bootstrap_codex_assets(target_repo_dir=target_repo)

            self.assertTrue(result.created_files)
            self.assertTrue(result.agents_created)
            self.assertTrue(has_required_codex_assets(target_repo))

            context = load_codex_preflight_context(target_repo)
            self.assertIn("Never add or expose secrets", context["policy"])
            self.assertIn("Read and follow", context["operating_or_agents"])
            self.assertEqual(context["operating_source"], "AGENTS.md")

    def test_codex_bootstrap_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            target_repo = Path(tmp_dir) / "target-repo"
            target_repo.mkdir(parents=True, exist_ok=True)

            first = bootstrap_codex_assets(target_repo_dir=target_repo)
            second = bootstrap_codex_assets(target_repo_dir=target_repo)

            self.assertTrue(first.created_files)
            self.assertFalse(second.created_files)
            self.assertTrue(second.already_bootstrapped)

    def test_codex_bootstrap_state_is_persisted_and_queryable(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/bootstrap_state.db"
            run_migrations(database_url=database_url)
            reset_db_engine_cache()
            session_factory = create_session_factory(database_url=database_url)

            with session_factory() as session:
                now = datetime.now(timezone.utc)
                session.add(
                    Tenant(
                        tenant_id="tenant-bootstrap",
                        name="Tenant Bootstrap",
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
                            "allowlist": ["https://github.com/example/repo"],
                            "mapping_rules_by_project_key": {"TP": "https://github.com/example/repo"},
                            "mapping_rules_by_component": {},
                            "fallback_repo": None,
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

            target_repo = Path(tmp_dir) / "target-repo"
            target_repo.mkdir(parents=True, exist_ok=True)

            with session_factory() as session:
                first = ensure_codex_bootstrap_state(
                    session=session,
                    tenant_id="tenant-bootstrap",
                    repo_url="https://github.com/example/repo",
                    target_repo_dir=target_repo,
                    run_id="run-001",
                    branch_name="jira/TP-1-bootstrap",
                )
                self.assertTrue(first.created_files)

                second = ensure_codex_bootstrap_state(
                    session=session,
                    tenant_id="tenant-bootstrap",
                    repo_url="https://github.com/example/repo",
                    target_repo_dir=target_repo,
                    run_id="run-002",
                    branch_name="jira/TP-2-feature",
                )
                self.assertFalse(second.created_files)
                self.assertTrue(second.already_bootstrapped)

                states = list_repo_bootstrap_states(
                    session=session,
                    tenant_id="tenant-bootstrap",
                )

            self.assertEqual(len(states), 1)
            self.assertEqual(states[0].repo_url, "https://github.com/example/repo")
            self.assertEqual(states[0].bootstrap_count, 1)
            self.assertIn(".codex/OPERATING.md", states[0].last_created_files)
