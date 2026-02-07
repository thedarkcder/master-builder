from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from orchestrator.core.policy_pack import (
    PolicyPack,
    find_banned_pattern_violations,
    select_policy_pack_for_files,
)
from orchestrator.tools.github_app import PullRequestFileChange


class PolicyPackTests(unittest.TestCase):
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
