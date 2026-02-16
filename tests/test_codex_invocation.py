from __future__ import annotations

import unittest
from unittest.mock import patch

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntime


class CodexInvocationTests(unittest.TestCase):
    def test_invoke_codex_json_flushes_invocation_logs(self) -> None:
        runtime = CodexRuntime(
            model="m",
            max_output_tokens=10,
            command="override",
            _request=lambda _s, _u, _w, _l=None: '{"ok": true}',
        )
        context = CodexInvocationContext(
            channel="worker",
            tenant_id="tenant-1",
            project_id="proj-1",
            command="workflow",
            stage="pm",
            working_dir=".",
            run_id="run-1",
        )

        class _Writer:
            def __init__(self) -> None:
                self.flushed_invocations: list[str] = []

            def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:
                _ = timeout_seconds
                self.flushed_invocations.append(invocation_id)

        writer = _Writer()
        with patch("orchestrator.core.codex_invocation._get_log_writer", return_value=writer):
            payload = invoke_codex_json(
                runtime=runtime,
                context=context,
                system_prompt="system",
                user_prompt="user",
            )

        self.assertEqual(payload, {"ok": True})
        self.assertEqual(len(writer.flushed_invocations), 1)
        self.assertTrue(writer.flushed_invocations[0].strip())


if __name__ == "__main__":
    unittest.main()
