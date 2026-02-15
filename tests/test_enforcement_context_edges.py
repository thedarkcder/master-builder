from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.enforcement_context import (
    EnforcementAssetsError,
    _load_assets_manifest,
    _read_packaged_asset_text,
    build_agent_enforcement_context,
)


class EnforcementContextEdgeTests(unittest.TestCase):
    def test_read_packaged_asset_text_returns_none_when_package_missing(self) -> None:
        with patch("orchestrator.core.enforcement_context.importlib.resources.files", side_effect=ModuleNotFoundError):
            self.assertIsNone(_read_packaged_asset_text("POLICY.md"))

    def test_read_packaged_asset_text_rejects_empty_content(self) -> None:
        fake_asset = SimpleNamespace(
            is_file=lambda: True,
            read_text=lambda encoding="utf-8": "   ",
        )
        fake_root = SimpleNamespace(joinpath=lambda _: fake_asset)
        with patch("orchestrator.core.enforcement_context.importlib.resources.files", return_value=fake_root):
            with self.assertRaisesRegex(EnforcementAssetsError, "empty"):
                _read_packaged_asset_text("POLICY.md")

    def test_load_assets_manifest_rejects_non_object_json(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            repo_root = Path(tmp_dir)
            codex_dir = repo_root / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            (codex_dir / "codex_assets_manifest.json").write_text('["bad"]', encoding="utf-8")
            with self.assertRaisesRegex(EnforcementAssetsError, "expected JSON object"):
                _load_assets_manifest(repo_root=repo_root)

    def test_build_enforcement_context_uses_packaged_policy_pack_when_local_missing(self) -> None:
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
            (codex_dir / "codex_assets_manifest.json").write_text(
                json.dumps({"assets_version": "1.2.3"}),
                encoding="utf-8",
            )

            fake_policy_resource = SimpleNamespace(
                name="policy_pack.python.json",
                read_text=lambda encoding="utf-8": '{"name":"python"}',
            )
            fake_packaged_dir = SimpleNamespace(
                iterdir=lambda: [fake_policy_resource],
                joinpath=lambda _name: SimpleNamespace(is_file=lambda: False),
            )
            with patch("orchestrator.core.enforcement_context.importlib.resources.files", return_value=fake_packaged_dir):
                context = build_agent_enforcement_context(repo_root=repo_root)
        self.assertIn("policy_pack.python.json", context)
        self.assertIn("Codex assets version: 1.2.3", context)

    def test_build_enforcement_context_includes_agents_and_skills(self) -> None:
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
            (codex_dir / "codex_assets_manifest.json").write_text(
                json.dumps({"assets_version": "1.2.3"}),
                encoding="utf-8",
            )
            (codex_dir / "policy_pack.python.json").write_text(
                json.dumps({"name": "python"}),
                encoding="utf-8",
            )
            (codex_dir / "skills" / "staff-engineer-review").mkdir(parents=True, exist_ok=True)
            (codex_dir / "skills" / "staff-engineer-review" / "SKILL.md").write_text(
                "repo skill content",
                encoding="utf-8",
            )
            user_home = Path(tmp_dir) / "user-home"
            (user_home / ".codex" / "skills" / "tests-flow").mkdir(parents=True, exist_ok=True)
            (user_home / ".codex" / "skills" / "tests-flow" / "SKILL.md").write_text(
                "user skill content",
                encoding="utf-8",
            )
            with patch("orchestrator.core.enforcement_context.Path.home", return_value=user_home):
                context = build_agent_enforcement_context(repo_root=repo_root)

        self.assertIn("AGENTS.md excerpt:", context)
        self.assertIn("agent instructions", context)
        self.assertIn(".codex/skills/staff-engineer-review/SKILL.md excerpt:", context)
        self.assertIn("repo skill content", context)
        self.assertIn("user-home/.codex/skills/tests-flow/SKILL.md excerpt:", context)
        self.assertIn("user skill content", context)
