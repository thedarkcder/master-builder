from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.core.enforcement_context import (
    EnforcementAssetsError,
    build_agent_enforcement_context,
)


class EnforcementContextEdgeTests(unittest.TestCase):
    def test_build_enforcement_context_rejects_missing_local_policy_pack(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            codex_dir = repo_root / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            for name in [
                "POLICY.md",
                "ENGINEERING_STANDARDS.md",
                "OPERATING.md",
                "DECISION_GATE_TEMPLATE.md",
                "PR_READY_TEMPLATES.md",
            ]:
                (codex_dir / name).write_text("content", encoding="utf-8")

            with self.assertRaisesRegex(EnforcementAssetsError, "Missing policy packs"):
                build_agent_enforcement_context(repo_root=repo_root)

    def test_build_enforcement_context_includes_agents_but_not_skills(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir) / "repo"
            repo_root.mkdir(parents=True, exist_ok=True)
            codex_dir = repo_root / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            for name in [
                "POLICY.md",
                "ENGINEERING_STANDARDS.md",
                "OPERATING.md",
                "DECISION_GATE_TEMPLATE.md",
                "PR_READY_TEMPLATES.md",
            ]:
                (codex_dir / name).write_text("content", encoding="utf-8")
            (repo_root / "AGENTS.md").write_text("agent instructions", encoding="utf-8")
            (codex_dir / "policy_pack.python.json").write_text(
                json.dumps({"name": "python"}),
                encoding="utf-8",
            )
            context = build_agent_enforcement_context(repo_root=repo_root)

        self.assertIn("AGENTS.md excerpt:", context)
        self.assertIn("agent instructions", context)
        self.assertNotIn("Loaded skills:", context)
