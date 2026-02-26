from __future__ import annotations

import unittest

from orchestrator.core.run_logs import extract_turn_completed_usage


class RunLogsUsageTests(unittest.TestCase):
    def test_extract_turn_completed_usage_preserves_total_tokens_without_split(self) -> None:
        parsed = extract_turn_completed_usage(
            '{"type":"turn.completed","usage":{"total_tokens":42}}'
        )
        assert parsed is not None
        self.assertEqual(parsed.input_tokens, 0)
        self.assertEqual(parsed.output_tokens, 42)
        self.assertEqual(parsed.cached_input_tokens, 0)

    def test_extract_turn_completed_usage_prefers_split_tokens_when_present(self) -> None:
        parsed = extract_turn_completed_usage(
            '{"type":"turn.completed","usage":{"input_tokens":12,"output_tokens":5,"total_tokens":99}}'
        )
        assert parsed is not None
        self.assertEqual(parsed.input_tokens, 12)
        self.assertEqual(parsed.output_tokens, 5)


if __name__ == "__main__":
    unittest.main()
