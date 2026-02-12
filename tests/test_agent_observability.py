import unittest
from datetime import datetime, timedelta, timezone

from orchestrator.core.agent_observability import (
    agent_observability_tracker,
    reset_agent_observability_for_tests,
)


class AgentObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_agent_observability_for_tests()

    def tearDown(self) -> None:
        reset_agent_observability_for_tests()

    def test_record_event_tracks_heartbeat_and_event_stream(self) -> None:
        now = datetime.now(timezone.utc)
        agent_observability_tracker.record_event(
            event_type="TASK_STARTED",
            tenant_id="tenant-a",
            project_id="tenant-a-default",
            run_id="run-1",
            issue_key="TP-1",
            agent_id="worker-1",
            recorded_at=now,
        )
        agent_observability_tracker.record_event(
            event_type="TASK_COMPLETED",
            tenant_id="tenant-a",
            project_id="tenant-a-default",
            run_id="run-1",
            issue_key="TP-1",
            agent_id="worker-1",
            recorded_at=now + timedelta(seconds=5),
        )
        events, heartbeats = agent_observability_tracker.snapshot()
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, "TASK_STARTED")
        self.assertEqual(events[1].event_type, "TASK_COMPLETED")
        self.assertIn(("tenant-a", "worker-1"), heartbeats)

    def test_unknown_event_type_is_normalized(self) -> None:
        agent_observability_tracker.record_event(
            event_type="not-known",
            tenant_id="tenant-a",
            project_id=None,
            run_id="run-2",
            issue_key=None,
            agent_id="worker-1",
        )
        events, _ = agent_observability_tracker.snapshot()
        self.assertEqual(events[0].event_type, "TASK_FAILED")


if __name__ == "__main__":
    unittest.main()
