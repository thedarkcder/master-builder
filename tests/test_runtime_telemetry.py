from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.runtime_telemetry import build_runtime_log_sink


class RuntimeTelemetryTests(unittest.TestCase):
    def test_runtime_telemetry_redacts_message_metadata(self) -> None:
        captured_extras: list[dict[str, object]] = []

        def _capture_log(message: str, *, extra: dict[str, object]) -> None:
            if message == "runtime_telemetry_line":
                captured_extras.append(extra)

        sink = build_runtime_log_sink(
            channel="worker",
            tenant_id="tenant-a",
            project_id="project-a",
            command="workflow.test",
            working_dir="/tmp/repo",
            issue_key="TA-1",
        )

        with patch("orchestrator.core.runtime_telemetry.logger.info", side_effect=_capture_log):
            sink(
                "stderr",
                "APP_STORE_CONNECT_API_KEY_BASE64=super-secret-value email=user@example.com",
            )

        self.assertEqual(len(captured_extras), 1)
        metadata = captured_extras[0]["metadata"]
        assert isinstance(metadata, dict)
        rendered = str(metadata["message"])
        self.assertNotIn("super-secret-value", rendered)
        self.assertNotIn("user@example.com", rendered)
        self.assertIn("[REDACTED]", rendered)


if __name__ == "__main__":
    unittest.main()
