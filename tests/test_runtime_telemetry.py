from __future__ import annotations

import builtins
import unittest
from unittest.mock import patch

import pytest

from orchestrator.core.observability.telemetry import build_runtime_log_sink
from orchestrator.core.observability.otel_telemetry import telemetry_span


class RuntimeTelemetryTests(unittest.TestCase):
    def test_runtime_telemetry_redacts_message_metadata(self) -> None:
        captured_extras: list[dict[str, object]] = []

        def _capture_log(message: str, *, extra: dict[str, object]) -> None:
            _ = message
            if extra.get("event_type") == "runtime_log":
                captured_extras.append(extra)

        sink = build_runtime_log_sink(
            channel="worker",
            tenant_id="tenant-a",
            project_id="project-a",
            command="workflow.test",
            working_dir="/tmp/repo",
            issue_key="TA-1",
        )

        with patch("orchestrator.core.observability.telemetry.logger.info", side_effect=_capture_log):
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


def test_telemetry_span_without_opentelemetry_preserves_body_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def _import_without_opentelemetry(name, globals=None, locals=None, fromlist=(), level=0):  # noqa: ANN001
        if str(name).startswith("opentelemetry"):
            raise ImportError("blocked optional opentelemetry import")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import_without_opentelemetry)

    with pytest.raises(RuntimeError) as exc_info:
        with telemetry_span("test"):
            raise RuntimeError("real workflow failure")

    assert str(exc_info.value) == "real workflow failure"
    assert not isinstance(exc_info.value.__context__, ImportError)


if __name__ == "__main__":
    unittest.main()
