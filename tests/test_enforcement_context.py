from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

        self.assertIn(".codex/POLICY.md excerpt:", context)
        self.assertIn(".codex/ENGINEERING_STANDARDS.md excerpt:", context)
        self.assertIn(".codex/OPERATING.md excerpt:", context)
        self.assertIn(".codex/DECISION_GATE_TEMPLATE.md excerpt:", context)
        self.assertIn(".codex/PR_READY_TEMPLATES.md excerpt:", context)
        self.assertIn("Loaded policy packs", context)
        self.assertIn("Codex assets version: 0.1.0", context)

    def test_validate_assets_raises_for_version_mismatch(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        with self.assertRaisesRegex(EnforcementAssetsError, "version mismatch"):
            validate_enforcement_assets(
                repo_root=repo_root,
                required_assets_version="9.9.9",
            )

    def test_codex_assets_version_uses_packaged_manifest_when_local_manifest_missing(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            with patch(
                "orchestrator.core.enforcement_context._read_packaged_asset_text",
                side_effect=lambda name: '{"assets_version":"0.2.0"}'
                if name == "codex_assets_manifest.json"
                else "content",
            ):
                version = codex_assets_version(repo_root=repo_root)
        self.assertEqual(version, "0.2.0")

    def test_validate_assets_raises_when_required_file_missing_and_packaged_unavailable(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            with patch(
                "orchestrator.core.enforcement_context._read_packaged_asset_text",
                return_value=None,
            ):
                with self.assertRaisesRegex(EnforcementAssetsError, "Missing required enforcement file"):
                    validate_enforcement_assets(repo_root=repo_root)
