from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.tools import stitch_tool
from orchestrator.tools.stitch_mcp_client import (
    StitchMcpError,
    parse_stitch_tool_mcp_result,
)


class StitchToolTests(unittest.TestCase):
    def test_maybe_invoke_skips_when_secret_missing(self) -> None:
        settings = SimpleNamespace(secrets_encryption_key="enc")
        with patch("orchestrator.core.config.get_settings", return_value=settings):
            with patch(
                "orchestrator.tools.stitch_tool._resolve_stitch_api_key_from_tenant_secret",
                return_value=None,
            ):
                out = stitch_tool.maybe_invoke_stitch_tool_for_stage_plan(
                    brief="hello",
                    tenant_id="t",
                    project_id="p",
                )
        self.assertIsNone(out)

    def test_maybe_invoke_appends_metadata_when_secret_resolves(self) -> None:
        settings = SimpleNamespace(secrets_encryption_key="enc")
        fake_result = {
            "provider": "stitch",
            "kind": "stitch_tool",
            "tool": "synthesize_screen",
        }
        with patch("orchestrator.core.config.get_settings", return_value=settings):
            with patch(
                "orchestrator.tools.stitch_tool._resolve_stitch_api_key_from_tenant_secret",
                return_value="api-key",
            ):
                with patch(
                    "orchestrator.tools.stitch_tool.synthesize_stitch_screen",
                    return_value=fake_result,
                ):
                    out = stitch_tool.maybe_invoke_stitch_tool_for_stage_plan(
                        brief="hello",
                        tenant_id="t",
                        project_id="p",
                    )
        self.assertIsNotNone(out)
        assert out is not None
        self.assertEqual(out["provider"], "stitch")
        self.assertEqual(out["kind"], "stitch_tool")
        self.assertEqual(out["tenant_id"], "t")
        self.assertEqual(out["project_id"], "p")
        self.assertIn("captured_at", out)

    def test_parse_stitch_tool_mcp_result_prefers_structured_content(self) -> None:
        out = parse_stitch_tool_mcp_result(
            {"structuredContent": {"x": 1}, "content": []},
            tool_name="t",
        )
        self.assertEqual(out, {"x": 1})

    def test_parse_stitch_tool_mcp_result_is_error_raises(self) -> None:
        with self.assertRaises(StitchMcpError):
            parse_stitch_tool_mcp_result(
                {
                    "isError": True,
                    "content": [{"type": "text", "text": "permission denied 403"}],
                },
                tool_name="t",
            )

    def test_synthesize_maps_mcp_error_to_stitch_tool_error(self) -> None:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.connect.side_effect = StitchMcpError("boom")
        with patch(
            "orchestrator.tools.stitch_tool.StitchMcpClient", return_value=mock_client
        ):
            with self.assertRaises(stitch_tool.StitchToolError):
                stitch_tool.synthesize_stitch_screen(api_key="k", prompt="p")

    def test_normalize_project_id_strips_prefix(self) -> None:
        self.assertEqual(
            stitch_tool._normalize_project_id({"name": "projects/abc"}), "abc"
        )
        self.assertEqual(stitch_tool._normalize_project_id("projects/xyz"), "xyz")


if __name__ == "__main__":
    unittest.main()
