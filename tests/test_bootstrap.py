from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.tools.bootstrap import bootstrap_ci_workflows


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
