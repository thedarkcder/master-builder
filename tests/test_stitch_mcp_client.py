from __future__ import annotations

import unittest

from orchestrator.tools.stitch_mcp_client import StitchMcpClient, _iter_sse_events


class StitchMcpClientTests(unittest.TestCase):
    def test_iter_sse_events_parses_blank_delimited_data(self) -> None:
        lines = ['data: {"jsonrpc":"2.0","id":1,"result":{}}', ""]
        msgs = list(_iter_sse_events(iter(lines)))
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0].get("id"), 1)
        self.assertEqual(msgs[0].get("result"), {})

    def test_json_rpc_result_dict_matches_id(self) -> None:
        client = StitchMcpClient("dummy-key")
        r = client._json_rpc_result_dict(  # noqa: SLF001
            request_id=2,
            payload={"jsonrpc": "2.0", "id": 2, "result": {"tools": []}},
        )
        self.assertEqual(r, {"tools": []})


if __name__ == "__main__":
    unittest.main()
