from __future__ import annotations

import unittest

from orchestrator.core.policy_pack import find_banned_pattern_violations, select_policy_pack_for_files
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
