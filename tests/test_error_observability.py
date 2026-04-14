import json
import sys
import unittest
from unittest.mock import patch

from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.observability import reset_log_context, set_log_context


class ErrorObservabilityTests(unittest.TestCase):
    def test_emit_hard_error_prints_structured_json_envelope(self) -> None:
        tokens = set_log_context(correlation_id="cid-1")
        try:
            with patch("builtins.print") as print_mock:
                emit_hard_error(
                    event="api_unhandled_exception",
                    error_ref="abc12345",
                    exc=RuntimeError("boom"),
                    context={"tenant_id": "tenant-1", "agent_id": "agent-1"},
                )
        finally:
            reset_log_context(tokens)

        self.assertEqual(print_mock.call_count, 2)
        first_line = print_mock.call_args_list[0].args[0]
        payload = json.loads(first_line)
        self.assertEqual(payload["event_type"], "api_unhandled_exception")
        self.assertEqual(payload["tenant_id"], "tenant-1")
        self.assertEqual(payload["agent_id"], "agent-1")
        self.assertEqual(payload["correlation_id"], "cid-1")
        self.assertEqual(payload["metadata"]["error_ref"], "abc12345")

    def test_emit_hard_error_captures_exception_in_sentry_when_available(self) -> None:
        class _FakeScope:
            def __init__(self) -> None:
                self.tags = {}
                self.contexts = {}

            def set_tag(self, key: str, value: str) -> None:
                self.tags[key] = value

            def set_context(self, key: str, value: dict) -> None:
                self.contexts[key] = value

        captured = {"exception": None, "scope": None}

        class _PushScope:
            def __enter__(self):  # noqa: ANN204
                scope = _FakeScope()
                captured["scope"] = scope
                return scope

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        class _FakeSentry:
            @staticmethod
            def push_scope():  # noqa: ANN205
                return _PushScope()

            @staticmethod
            def capture_exception(exc: Exception) -> None:
                captured["exception"] = exc

        with (
            patch("builtins.print"),
            patch.dict(sys.modules, {"sentry_sdk": _FakeSentry()}),
        ):
            error = RuntimeError("boom")
            emit_hard_error(
                event="discord_command_followup_send_failed",
                error_ref="ref-1",
                exc=error,
                context={"tenant_id": "example"},
            )

        self.assertIs(captured["exception"], error)
        self.assertIsNotNone(captured["scope"])
        self.assertEqual(captured["scope"].tags["event_type"], "discord_command_followup_send_failed")
        self.assertEqual(captured["scope"].tags["error_ref"], "ref-1")


if __name__ == "__main__":
    unittest.main()
