from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.core.enforcement_context import (
    EnforcementAssetsError,
    build_agent_enforcement_context,
    validate_enforcement_assets,
)


class EnforcementContextTests(unittest.TestCase):
    def test_enforcement_context_includes_required_rules(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        context = build_agent_enforcement_context(repo_root=repo_root)

        self.assertIn(".codex/POLICY.md excerpt:", context)
        self.assertIn(".codex/ENGINEERING_STANDARDS.md excerpt:", context)
        self.assertIn(".codex/OPERATING.md excerpt:", context)
        self.assertIn(".codex/DECISION_GATE_TEMPLATE.md excerpt:", context)
        self.assertIn(".codex/PR_READY_TEMPLATES.md excerpt:", context)
        self.assertIn("AGENTS.md excerpt:", context)
        self.assertIn("Loaded policy packs", context)

    def test_validate_assets_raises_when_required_file_missing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            with self.assertRaisesRegex(EnforcementAssetsError, "Missing required enforcement file"):
                validate_enforcement_assets(repo_root=repo_root)
