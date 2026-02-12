from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from orchestrator.core.policy_pack import (
    PolicyPack,
    _detect_language_key_for_filename,
    find_banned_pattern_violations,
    load_policy_pack,
    select_policy_pack_for_files,
)
from orchestrator.tools.github_app import PullRequestFileChange


class PolicyPackTests(unittest.TestCase):
    def test_load_policy_pack_validation_errors(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            missing_path = tmp / "policy_pack.python.json"
            with self.assertRaises(FileNotFoundError):
                load_policy_pack(language_key="python", policy_pack_dir=tmp)

            missing_path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                load_policy_pack(language_key="python", policy_pack_dir=tmp)

            missing_path.write_text('{"name":"","primary_language":"python","banned_patterns":["x"]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing 'name'"):
                load_policy_pack(language_key="python", policy_pack_dir=tmp)

            missing_path.write_text('{"name":"python","primary_language":"","banned_patterns":["x"]}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing 'primary_language'"):
                load_policy_pack(language_key="python", policy_pack_dir=tmp)

            missing_path.write_text('{"name":"python","primary_language":"python"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing 'banned_patterns'"):
                load_policy_pack(language_key="python", policy_pack_dir=tmp)

            missing_path.write_text(
                '{"name":"python","primary_language":"python","banned_patterns":[" ",""],"preferred_patterns":"x","reviewer_quality_gates":"x"}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "no banned patterns"):
                load_policy_pack(language_key="python", policy_pack_dir=tmp)

            missing_path.write_text(
                '{"name":"python","primary_language":"python","banned_patterns":["sleep"],"preferred_patterns":["a"," "],"reviewer_quality_gates":["gate",""]}',
                encoding="utf-8",
            )
            loaded = load_policy_pack(language_key="python", policy_pack_dir=tmp)
            self.assertEqual(loaded.preferred_patterns, ("a",))
            self.assertEqual(loaded.reviewer_quality_gates, ("gate",))

    def test_detect_language_key_for_filename(self) -> None:
        self.assertEqual(_detect_language_key_for_filename("src/app.py"), "python")
        self.assertEqual(_detect_language_key_for_filename("admin-ui/src/App.tsx"), "react")
        self.assertEqual(_detect_language_key_for_filename("services/frontend/index.ts"), "react")
        self.assertEqual(_detect_language_key_for_filename("server/index.ts"), "node")
        self.assertIsNone(_detect_language_key_for_filename("README.md"))

    def test_select_policy_pack_prefers_react_for_tsx_changes(self) -> None:
        selected = select_policy_pack_for_files(
            files=[
                PullRequestFileChange(
                    filename="admin-ui/src/components/TenantWizard.tsx",
                    patch="+ const x = 1;",
                )
            ]
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.language_key, "react")

    def test_select_policy_pack_returns_none_when_no_supported_file_or_missing_pack(self) -> None:
        self.assertIsNone(
            select_policy_pack_for_files(
                files=[PullRequestFileChange(filename="README.md", patch="+ docs")],
            )
        )
        with TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            # Deliberately omit react policy file to force selection skip.
            (tmp / "policy_pack.python.json").write_text(
                '{"name":"python","primary_language":"python","banned_patterns":["sleep"]}',
                encoding="utf-8",
            )
            self.assertIsNone(
                select_policy_pack_for_files(
                    files=[PullRequestFileChange(filename="admin-ui/src/App.tsx", patch="+ const x = 1;")],
                    policy_pack_dir=tmp,
                )
            )

    def test_find_banned_pattern_violations_detects_react_timeout_hack(self) -> None:
        selected = select_policy_pack_for_files(
            files=[
                PullRequestFileChange(
                    filename="admin-ui/src/components/TenantWizard.tsx",
                    patch="+ await new Promise((resolve) => setTimeout(resolve, 500));",
                )
            ]
        )
        self.assertIsNotNone(selected)
        assert selected is not None

        violations = find_banned_pattern_violations(
            policy_pack=selected,
            files=[
                PullRequestFileChange(
                    filename="admin-ui/src/components/TenantWizard.tsx",
                    patch="+ await new Promise((resolve) => setTimeout(resolve, 500));",
                )
            ],
        )

        self.assertTrue(violations)
        self.assertIn("TenantWizard.tsx", violations[0])

    def test_find_banned_pattern_violations_reports_invalid_regex(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            policy_pack = PolicyPack(
                language_key="python",
                name="python",
                primary_language="python",
                banned_patterns=("(",),
                preferred_patterns=(),
                reviewer_quality_gates=(),
                source_path=Path(tmp_dir) / "policy_pack.python.json",
            )

            violations = find_banned_pattern_violations(
                policy_pack=policy_pack,
                files=[
                    PullRequestFileChange(
                        filename="orchestrator/worker.py",
                        patch="+ print('ok')",
                    )
                ],
            )

        self.assertEqual(len(violations), 1)
        self.assertIn("invalid banned pattern", violations[0])

    def test_find_banned_pattern_violations_ignores_removed_lines(self) -> None:
        selected = select_policy_pack_for_files(
            files=[
                PullRequestFileChange(
                    filename="admin-ui/src/components/TenantWizard.tsx",
                    patch="@@ -1,2 +1,2 @@",
                )
            ]
        )
        self.assertIsNotNone(selected)
        assert selected is not None

        violations = find_banned_pattern_violations(
            policy_pack=selected,
            files=[
                PullRequestFileChange(
                    filename="admin-ui/src/components/TenantWizard.tsx",
                    patch=(
                        "@@ -10,2 +10,2 @@\n"
                        "- await new Promise((resolve) => setTimeout(resolve, 500));\n"
                        "+ await doWorkWithoutSleep();\n"
                    ),
                )
            ],
        )

        self.assertEqual(violations, [])

    def test_find_banned_pattern_violations_skips_empty_patch(self) -> None:
        selected = select_policy_pack_for_files(
            files=[PullRequestFileChange(filename="orchestrator/worker.py", patch="+ import time")],
        )
        self.assertIsNotNone(selected)
        assert selected is not None

        violations = find_banned_pattern_violations(
            policy_pack=selected,
            files=[PullRequestFileChange(filename="orchestrator/worker.py", patch=None)],
        )
        self.assertEqual(violations, [])
