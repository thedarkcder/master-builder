from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_discord_command_handlers_do_not_import_provider_clients_directly(self) -> None:
        command_modules = sorted((ROOT / "orchestrator" / "api").glob("discord_command_*.py"))
        self.assertTrue(command_modules)

        violations: list[str] = []
        for module_path in command_modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("orchestrator.tools"):
                    violations.append(f"{module_path.name}:{node.lineno}:{node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("orchestrator.tools"):
                            violations.append(f"{module_path.name}:{node.lineno}:{alias.name}")

        self.assertEqual(
            violations,
            [],
            msg=f"Direct provider imports found in command handlers: {violations}",
        )


if __name__ == "__main__":
    unittest.main()
