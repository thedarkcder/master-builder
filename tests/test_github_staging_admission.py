from __future__ import annotations

import unittest
from types import SimpleNamespace

from orchestrator.api.webhooks.github_staging_admission import (
    STAGING_MERGE_CHECK_NAME,
    plan_staging_admission_actions,
    resolve_staging_admission_config,
)


class GitHubStagingAdmissionTests(unittest.TestCase):
    def test_resolve_staging_admission_config_normalizes_enabled_and_branch(
        self,
    ) -> None:
        config = resolve_staging_admission_config(
            {
                "staging_admission_enabled": True,
                "staging_branch": " stage ",
            }
        )

        self.assertTrue(config.enabled)
        self.assertEqual(config.branch, "stage")

    def test_pull_request_targeting_staging_publishes_success_check(self) -> None:
        github_client = SimpleNamespace(
            get_pull_request_details=lambda **_: SimpleNamespace(
                number=17,
                state="open",
                title="GP-17: example",
                head_sha="abc123",
                base_ref="staging",
                mergeable=True,
                mergeable_state="clean",
            ),
            get_branch_head_sha=lambda **_: "base4567890",
            list_open_pull_requests=lambda **_: [],
        )

        plan = plan_staging_admission_actions(
            github_event="pull_request",
            normalized_action="opened",
            payload={},
            repo_full_name="org/repo",
            project_overrides={
                "staging_admission_enabled": True,
                "staging_branch": "staging",
            },
            pr_targets=[(17, True)],
            github_client=github_client,
        )

        self.assertTrue(plan.enabled)
        self.assertEqual(len(plan.actions), 1)
        action = plan.actions[0]
        self.assertEqual(action.repo_full_name, "org/repo")
        self.assertEqual(action.head_sha, "abc123")
        self.assertEqual(action.name, STAGING_MERGE_CHECK_NAME)
        self.assertEqual(action.status, "completed")
        self.assertEqual(action.conclusion, "success")
        self.assertIn("staging", action.summary)
        self.assertIn("base4567890"[:7], action.summary)

    def test_pull_request_conflict_publishes_failure_check(self) -> None:
        github_client = SimpleNamespace(
            get_pull_request_details=lambda **_: SimpleNamespace(
                number=22,
                state="open",
                title="GP-22: conflict",
                head_sha="def456",
                base_ref="staging",
                mergeable=False,
                mergeable_state="dirty",
            ),
            get_branch_head_sha=lambda **_: "base4567890",
            list_open_pull_requests=lambda **_: [],
        )

        plan = plan_staging_admission_actions(
            github_event="pull_request",
            normalized_action="synchronize",
            payload={},
            repo_full_name="org/repo",
            project_overrides={
                "staging_admission_enabled": True,
                "staging_branch": "staging",
            },
            pr_targets=[(22, True)],
            github_client=github_client,
        )

        self.assertEqual(len(plan.actions), 1)
        action = plan.actions[0]
        self.assertEqual(action.conclusion, "failure")
        self.assertIn("not mergeable", action.summary)
        self.assertIn("dirty", action.summary)

    def test_push_to_staging_rechecks_all_open_staging_pull_requests(self) -> None:
        open_prs = [
            SimpleNamespace(
                number=31,
                head_ref="feature/a",
                base_ref="staging",
                title="A",
                state="open",
                html_url="",
            ),
            SimpleNamespace(
                number=32,
                head_ref="feature/b",
                base_ref="main",
                title="B",
                state="open",
                html_url="",
            ),
            SimpleNamespace(
                number=33,
                head_ref="feature/c",
                base_ref="staging",
                title="C",
                state="open",
                html_url="",
            ),
        ]
        details_by_number = {
            31: SimpleNamespace(
                number=31,
                state="open",
                title="A",
                head_sha="sha31",
                base_ref="staging",
                mergeable=True,
                mergeable_state="clean",
            ),
            33: SimpleNamespace(
                number=33,
                state="open",
                title="C",
                head_sha="sha33",
                base_ref="staging",
                mergeable=False,
                mergeable_state="dirty",
            ),
        }
        github_client = SimpleNamespace(
            get_pull_request_details=lambda **kwargs: details_by_number[
                kwargs["pr_number"]
            ],
            get_branch_head_sha=lambda **_: "staging999",
            list_open_pull_requests=lambda **_: open_prs,
        )

        plan = plan_staging_admission_actions(
            github_event="push",
            normalized_action=None,
            payload={"ref": "refs/heads/staging"},
            repo_full_name="org/repo",
            project_overrides={
                "staging_admission_enabled": True,
                "staging_branch": "staging",
            },
            pr_targets=[],
            github_client=github_client,
        )

        self.assertEqual(
            {action.head_sha for action in plan.actions}, {"sha31", "sha33"}
        )
        self.assertEqual({result["pr_number"] for result in plan.results}, {31, 33})
