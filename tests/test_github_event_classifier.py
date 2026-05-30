from __future__ import annotations

import unittest

from orchestrator.api.webhooks.github_event_classifier import classify_github_trigger_state


class GitHubEventClassifierTests(unittest.TestCase):
    def test_pull_request_event_triggers_full_review(self) -> None:
        state = classify_github_trigger_state(
            github_event="pull_request",
            normalized_action="opened",
            payload={},
        )

        self.assertTrue(state.full_review_trigger)
        self.assertFalse(state.remediation_trigger)

    def test_pull_request_closed_does_not_trigger_full_review(self) -> None:
        state = classify_github_trigger_state(
            github_event="pull_request",
            normalized_action="closed",
            payload={},
        )

        self.assertFalse(state.full_review_trigger)
        self.assertFalse(state.remediation_trigger)

    def test_check_run_event_triggers_remediation_but_not_full_review(self) -> None:
        state = classify_github_trigger_state(
            github_event="check_run",
            normalized_action="completed",
            payload={"check_run": {"conclusion": "failure"}},
        )

        self.assertFalse(state.full_review_trigger)
        self.assertTrue(state.remediation_trigger)

    def test_check_suite_event_triggers_remediation_but_not_full_review(self) -> None:
        state = classify_github_trigger_state(
            github_event="check_suite",
            normalized_action="completed",
            payload={"check_suite": {"conclusion": "timed_out"}},
        )

        self.assertFalse(state.full_review_trigger)
        self.assertTrue(state.remediation_trigger)


if __name__ == "__main__":
    unittest.main()
