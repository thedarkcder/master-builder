from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.core.enforcement_context import build_agent_enforcement_context


class EnforcementContextTests(unittest.TestCase):
    def test_enforcement_context_includes_required_rules(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        context = build_agent_enforcement_context(repo_root=repo_root)

        self.assertIn(".codex/POLICY.md excerpt:", context)
        self.assertIn(".codex/ENGINEERING_STANDARDS.md excerpt:", context)
        self.assertIn(".codex/OPERATING.md excerpt:", context)
        self.assertIn(".codex/DECISION_GATE_TEMPLATE.md excerpt:", context)
        self.assertIn(".codex/PR_READY_TEMPLATES.md excerpt:", context)
        self.assertIn("Loaded policy packs", context)

    def test_enforcement_context_uses_fallback_when_required_codex_file_missing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            codex_dir = repo_root / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            (codex_dir / "POLICY.md").write_text("# Policy\n", encoding="utf-8")
            (codex_dir / "ENGINEERING_STANDARDS.md").write_text("# Engineering\n", encoding="utf-8")
            (codex_dir / "OPERATING.md").write_text("# Operating\n", encoding="utf-8")

            context = build_agent_enforcement_context(repo_root=repo_root)

            self.assertIn("Fallback Decision Gate template", context)
            self.assertIn("Fallback PR Ready template", context)

    def test_enforcement_context_uses_fallback_when_codex_directory_missing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            context = build_agent_enforcement_context(repo_root=repo_root)

            self.assertIn("Fallback POLICY guidance", context)
            self.assertIn("Fallback ENGINEERING_STANDARDS guidance", context)
            self.assertIn("Fallback Decision Gate template", context)
