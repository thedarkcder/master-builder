import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from orchestrator.core.observability.alerting import AlertCandidate, AlertDedupRegistry


class AlertingTests(unittest.TestCase):
    def test_filter_candidates_is_thread_safe_under_parallel_access(self) -> None:
        registry = AlertDedupRegistry()
        now = datetime.now(timezone.utc)
        candidates = [
            AlertCandidate(
                alert_key=f"tenant:tenant-{index}:jira_disconnected",
                severity="HIGH",
                scope_type="tenant",
                scope_id=f"tenant-{index}",
                reason="Atlassian integration is not connected.",
            )
            for index in range(10)
        ]

        def _invoke(prune_missing_keys: bool) -> int:
            emitted = registry.filter_candidates(
                candidates=candidates,
                now=now,
                cooldown_seconds=600,
                prune_missing_keys=prune_missing_keys,
            )
            return len(emitted)

        with ThreadPoolExecutor(max_workers=16) as executor:
            futures = [
                executor.submit(_invoke, index % 2 == 0)
                for index in range(200)
            ]
            results = [future.result() for future in as_completed(futures)]

        self.assertEqual(len(results), 200)


if __name__ == "__main__":
    unittest.main()
