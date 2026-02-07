from __future__ import annotations

import unittest
from pathlib import Path

from orchestrator.core.enforcement_context import build_agent_enforcement_context


class EnforcementContextTests(unittest.TestCase):
    def test_enforcement_context_includes_required_rules(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        context = build_agent_enforcement_context(repo_root=repo_root)

        self.assertIn("Good To Do checklist", context)
        self.assertIn("Decision Gate", context)
        self.assertIn("No placeholders policy", context)
        self.assertIn("Loaded policy packs", context)
