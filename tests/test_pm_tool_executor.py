from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.pm_tool_executor import execute_pm_tool_calls
from orchestrator.core.runtime_payload_models import PMToolCallPayload


class PmToolExecutorTests(unittest.TestCase):
    def test_rejects_unknown_tools(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "PM tool call uses unknown tool"):
            execute_pm_tool_calls(
                tool_calls=[PMToolCallPayload(tool="unknown.tool", arguments={"x": 1})],
                tenant_id="t1",
                project_id="p1",
            )

    def test_executes_stitch_synthesize_when_allowlisted(self) -> None:
        fake = {"provider": "stitch", "kind": "stitch_tool", "tool": "synthesize_screen"}
        with patch(
            "orchestrator.core.pm_tool_executor.maybe_invoke_stitch_tool_for_stage_plan",
            return_value=fake,
        ) as call:
            outputs = execute_pm_tool_calls(
                tool_calls=[PMToolCallPayload(tool="stitch.synthesize_screen", arguments={"prompt": "hero layout"})],
                tenant_id="t1",
                project_id="p1",
            )
        call.assert_called_once()
        self.assertEqual(outputs, [fake])


if __name__ == "__main__":
    unittest.main()
