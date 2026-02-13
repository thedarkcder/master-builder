import json
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


if __name__ == "__main__":
    unittest.main()
