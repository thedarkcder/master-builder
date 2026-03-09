from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_ROOT = ROOT / "orchestrator"

# Explicit allowlist for intentionally permitted route coupling.
# The test fails on any new coupling or stale expectation drift.
LEGACY_ROUTE_IMPORT_ALLOWLIST = {
    "orchestrator/core/discord/gateway_runtime.py": {
        "orchestrator.api.routes.discord",
    },
    "orchestrator/api/routes/discord.py": set(),
    "orchestrator/api/routes/webhook.py": set(),
    "orchestrator/api/routes/webhook_discord_interactions.py": set(),
    "orchestrator/api/routes/webhook_github.py": set(),
}
NON_ROUTE_API_ROUTE_IMPORT_ALLOWLIST = {
    "orchestrator/api/main.py": {
        "orchestrator.api.routes.admin",
        "orchestrator.api.routes.admin_auth",
        "orchestrator.api.routes.admin_knowledge",
        "orchestrator.api.routes.admin_observability",
        "orchestrator.api.routes.admin_runs",
        "orchestrator.api.routes.admin_secrets",
        "orchestrator.api.routes.admin_tokens",
        "orchestrator.api.routes.discord",
        "orchestrator.api.routes.runs",
        "orchestrator.api.routes.webhook",
        "orchestrator.api.routes.webhook_discord",
        "orchestrator.api.routes.webhook_discord_interactions",
        "orchestrator.api.routes.webhook_github",
    },
}


def _imported_modules(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
    return imports


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_discord_command_handlers_do_not_import_provider_clients_directly(self) -> None:
        command_modules = sorted((ROOT / "orchestrator" / "api" / "discord" / "commands").glob("*.py"))
        self.assertTrue(command_modules)

        violations: list[str] = []
        for module_path in command_modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith("orchestrator.tools"):
                        violations.append(f"{module_path.name}:{node.lineno}:{node.module}")
                    if node.module.startswith("orchestrator.api.routes."):
                        violations.append(f"{module_path.name}:{node.lineno}:{node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("orchestrator.tools"):
                            violations.append(f"{module_path.name}:{node.lineno}:{alias.name}")
                        if alias.name.startswith("orchestrator.api.routes."):
                            violations.append(f"{module_path.name}:{node.lineno}:{alias.name}")

        self.assertEqual(
            violations,
            [],
            msg=f"Direct provider imports found in command handlers: {violations}",
        )

    def test_core_modules_do_not_import_api_routes_outside_allowlist(self) -> None:
        core_modules = sorted(ORCHESTRATOR_ROOT.rglob("core/**/*.py"))
        self.assertTrue(core_modules)
        violations: list[str] = []
        observed_allowed: set[tuple[str, str]] = set()
        for module_path in core_modules:
            relative = module_path.relative_to(ROOT).as_posix()
            allowed = LEGACY_ROUTE_IMPORT_ALLOWLIST.get(relative, set())
            for module_name in _imported_modules(module_path):
                if not module_name.startswith("orchestrator.api.routes."):
                    continue
                if module_name in allowed:
                    observed_allowed.add((relative, module_name))
                    continue
                violations.append(f"{relative}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Core modules importing API routes outside allowlist: {violations}",
        )
        expected_allowed = {
            (path, module_name)
            for path, module_names in LEGACY_ROUTE_IMPORT_ALLOWLIST.items()
            if path.startswith("orchestrator/core/")
            for module_name in module_names
        }
        self.assertEqual(
            observed_allowed,
            expected_allowed,
            msg="Core route import allowlist drifted; update list only with explicit architectural decision.",
        )

    def test_route_modules_only_use_explicit_route_import_allowlist(self) -> None:
        route_modules = sorted((ORCHESTRATOR_ROOT / "api" / "routes").rglob("*.py"))
        self.assertTrue(route_modules)
        violations: list[str] = []
        observed_allowed: set[tuple[str, str]] = set()
        for module_path in route_modules:
            relative = module_path.relative_to(ROOT).as_posix()
            allowed = LEGACY_ROUTE_IMPORT_ALLOWLIST.get(relative, set())
            for module_name in _imported_modules(module_path):
                if not module_name.startswith("orchestrator.api.routes."):
                    continue
                if module_name == relative.replace("/", ".")[:-3]:
                    continue
                if module_name in allowed:
                    observed_allowed.add((relative, module_name))
                    continue
                violations.append(f"{relative}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Route-to-route imports outside allowlist: {violations}",
        )
        expected_allowed = {
            (path, module_name)
            for path, module_names in LEGACY_ROUTE_IMPORT_ALLOWLIST.items()
            if path.startswith("orchestrator/api/")
            for module_name in module_names
        }
        self.assertEqual(
            observed_allowed,
            expected_allowed,
            msg="Route import allowlist drifted; update list only with explicit architectural decision.",
        )

    def test_non_route_api_modules_only_use_explicit_route_import_allowlist(self) -> None:
        api_modules = sorted(ORCHESTRATOR_ROOT.rglob("api/*.py"))
        self.assertTrue(api_modules)
        violations: list[str] = []
        observed_allowed: set[tuple[str, str]] = set()
        for module_path in api_modules:
            relative = module_path.relative_to(ROOT).as_posix()
            if relative.startswith("orchestrator/api/routes/"):
                continue
            allowed = NON_ROUTE_API_ROUTE_IMPORT_ALLOWLIST.get(relative, set())
            for module_name in _imported_modules(module_path):
                if not module_name.startswith("orchestrator.api.routes."):
                    continue
                if module_name in allowed:
                    observed_allowed.add((relative, module_name))
                    continue
                violations.append(f"{relative}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Non-route API module imports routes outside allowlist: {violations}",
        )
        expected_allowed = {
            (path, module_name)
            for path, module_names in NON_ROUTE_API_ROUTE_IMPORT_ALLOWLIST.items()
            for module_name in module_names
        }
        self.assertEqual(
            observed_allowed,
            expected_allowed,
            msg="Non-route API allowlist drifted; update list only with explicit architectural decision.",
        )


if __name__ == "__main__":
    unittest.main()
