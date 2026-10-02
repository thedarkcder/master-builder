from __future__ import annotations

import unittest

from orchestrator.core.pm.plugin_catalog import (
    plugin_catalog_payload,
    tool_catalog_payload,
)


class PmPluginCatalogTests(unittest.TestCase):
    def test_plugin_catalog_contains_design(self) -> None:
        plugins = plugin_catalog_payload()
        plugin_ids = {str(item.get("plugin_id") or "") for item in plugins}
        self.assertIn("design", plugin_ids)

    def test_tool_catalog_contains_stitch_synthesize(self) -> None:
        tools = tool_catalog_payload()
        names = {str(item.get("tool_name") or "") for item in tools}
        self.assertIn("stitch.synthesize_screen", names)


if __name__ == "__main__":
    unittest.main()
