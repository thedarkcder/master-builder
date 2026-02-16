from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from orchestrator.core.enforcement_context import (
    EnforcementAssetsError,
    build_agent_enforcement_context,
    codex_assets_version,
    validate_enforcement_assets,
)


class EnforcementContextTests(unittest.TestCase):
    def test_enforcement_context_includes_required_rules(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        context = build_agent_enforcement_context(repo_root=repo_root)
        expected_version = json.loads(
            (repo_root / ".codex" / "codex_assets_manifest.json").read_text(encoding="utf-8")
        )["assets_version"]

        self.assertIn(".codex/POLICY.md excerpt:", context)
        self.assertIn(".codex/ENGINEERING_STANDARDS.md excerpt:", context)
        self.assertIn(".codex/OPERATING.md excerpt:", context)
        self.assertIn(".codex/DECISION_GATE_TEMPLATE.md excerpt:", context)
        self.assertIn(".codex/PR_READY_TEMPLATES.md excerpt:", context)
        self.assertIn("AGENTS.md excerpt:", context)
        self.assertIn("Loaded policy packs", context)
        self.assertIn(f"Codex assets version: {expected_version}", context)

    def test_validate_assets_raises_for_version_mismatch(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with self.assertRaisesRegex(EnforcementAssetsError, "version mismatch"):
            validate_enforcement_assets(
                repo_root=repo_root,
                required_assets_version="9.9.9",
            )

    def test_codex_assets_version_requires_local_manifest(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            with self.assertRaisesRegex(EnforcementAssetsError, "Missing required enforcement file"):
                codex_assets_version(repo_root=repo_root)

    def test_validate_assets_raises_when_required_file_missing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            with self.assertRaisesRegex(EnforcementAssetsError, "Missing required enforcement file"):
                validate_enforcement_assets(repo_root=repo_root)
