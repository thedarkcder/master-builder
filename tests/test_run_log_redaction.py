from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.observability.logging_pane import emit_logging_pane_event
from orchestrator.core.observability.repository import (
    configure_product_event_repository_for_tests,
)
from tests.test_support.product_events import RecordingProductEventRepository


class RunLogRedactionTests(unittest.TestCase):
    def test_logging_pane_event_persists_redacted_messages(self) -> None:
        message = (
            "run_id=123e4567-e89b-12d3-a456-426614174000 "
            "APP_STORE_CONNECT_API_KEY_BASE64=super-secret-value "
            "email=user@example.com "
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        )
        repository = RecordingProductEventRepository()

        with patch(
            "orchestrator.core.observability.writer.publish_product_event_notification",
            lambda **_kwargs: None,
        ):
            configure_product_event_repository_for_tests(repository)
            emit_logging_pane_event(
                session=object(),
                tenant_id="tenant-a",
                project_id="project-a",
                run_id="run-1",
                issue_key="TA-1",
                agent_id="worker-1",
                invocation_id="inv-1",
                channel="worker",
                command="workflow.test",
                working_dir="/tmp/repo",
                stage="test",
                attempt=1,
                stream="stderr",
                message=message,
            )

        self.assertEqual(len(repository.inserted), 1)
        persisted = (
            f"{repository.inserted[0].message} {repository.inserted[0].payload_json}"
        )
        self.assertNotIn("super-secret-value", persisted)
        self.assertNotIn("user@example.com", persisted)
        self.assertNotIn("BEGIN PRIVATE KEY", persisted)
        self.assertIn("123e4567-e89b-12d3-a456-426614174000", persisted)
        self.assertIn("[REDACTED]", persisted)


if __name__ == "__main__":
    unittest.main()
