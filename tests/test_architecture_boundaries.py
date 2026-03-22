from __future__ import annotations

import ast
from dataclasses import fields
from pathlib import Path
import unittest

from orchestrator.core.communications.contracts import (
    DiscordAskWithThreadAction,
    DiscordSeedWithThreadAction,
    DiscordThreadReplyAction,
)


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_ROOT = ROOT / "orchestrator"

# Explicit allowlist for intentionally permitted route coupling.
# The test fails on any new coupling or stale expectation drift.
LEGACY_ROUTE_IMPORT_ALLOWLIST = {
    "orchestrator/core/discord/gateway_runtime.py": set(),
    "orchestrator/api/routes/discord.py": set(),
    "orchestrator/api/routes/webhook.py": set(),
    "orchestrator/api/routes/webhook_discord_interactions.py": set(),
    "orchestrator/api/routes/webhook_github.py": set(),
}
NON_ROUTE_API_ROUTE_IMPORT_ALLOWLIST = {
    "orchestrator/api/main.py": {
        "orchestrator.api.routes.admin_auth",
        "orchestrator.api.routes.admin_codex",
        "orchestrator.api.routes.admin_discord_commands",
        "orchestrator.api.routes.admin_discord_allowlist",
        "orchestrator.api.routes.admin_github",
        "orchestrator.api.routes.admin_jira",
        "orchestrator.api.routes.admin_knowledge",
        "orchestrator.api.routes.admin_observability",
        "orchestrator.api.routes.admin_ready",
        "orchestrator.api.routes.admin_release",
        "orchestrator.api.routes.admin_runs",
        "orchestrator.api.routes.admin_secrets",
        "orchestrator.api.routes.admin_tenants",
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
    def test_discord_thread_actions_do_not_embed_runtime_objects(self) -> None:
        runtime_field_names = {"session", "settings", "tenant"}
        for action_type in (DiscordThreadReplyAction, DiscordAskWithThreadAction, DiscordSeedWithThreadAction):
            self.assertTrue(
                runtime_field_names.isdisjoint({field.name for field in fields(action_type)}),
                msg=f"{action_type.__name__} still embeds runtime objects in transport action fields",
            )

    def test_production_modules_do_not_construct_raw_transport_action(self) -> None:
        production_modules = sorted(ORCHESTRATOR_ROOT.rglob("*.py"))
        self.assertTrue(production_modules)

        violations: list[str] = []
        for module_path in production_modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id == "TransportAction":
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}")
        self.assertEqual(
            violations,
            [],
            msg=f"Raw TransportAction construction found; use typed transport actions instead: {violations}",
        )

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

    def test_webhook_classifier_and_planner_modules_do_not_import_provider_clients(self) -> None:
        planner_modules = sorted((ROOT / "orchestrator" / "api" / "webhooks").glob("*_planner.py"))
        classifier_modules = sorted((ROOT / "orchestrator" / "api" / "webhooks").glob("*_classifier.py"))
        modules = planner_modules + classifier_modules
        self.assertTrue(modules)

        violations: list[str] = []
        for module_path in modules:
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
            msg=f"Webhook planner/classifier modules importing provider clients directly: {violations}",
        )

    def test_github_and_jira_application_roots_do_not_import_provider_helpers_directly(self) -> None:
        banned_imports = {
            "orchestrator.api.webhooks.pr_review_comment_service",
            "orchestrator.core.discord.notifications",
            "orchestrator.api.webhooks.jira_webhook_board_gate",
            "orchestrator.api.webhooks.jira_webhook_comment_flow",
        }
        modules = [
            ROOT / "orchestrator" / "api" / "webhooks" / "github_application.py",
            ROOT / "orchestrator" / "api" / "webhooks" / "jira_application.py",
        ]
        violations: list[str] = []
        for module_path in modules:
            for module_name in _imported_modules(module_path):
                if module_name in banned_imports:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Application roots importing provider helpers directly: {violations}",
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

    def test_http_transport_adapters_do_not_import_provider_executors_directly(self) -> None:
        adapter_modules = sorted((ROOT / "orchestrator" / "api" / "routes").glob("webhook*.py")) + sorted(
            (ROOT / "orchestrator" / "api" / "webhooks").glob("*_ingress.py")
        )
        banned_imports = {
            "orchestrator.core.discord.transport_executor",
            "orchestrator.core.github.transport_executor",
        }
        violations: list[str] = []
        for module_path in adapter_modules:
            for module_name in _imported_modules(module_path):
                if module_name in banned_imports:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"HTTP transport adapters should not import provider executors directly: {violations}",
        )

    def test_production_modules_do_not_reference_reply_transport_compatibility_shims(self) -> None:
        banned_names = {"InteractiveReplyTransport", "DiscordReplyTransport"}
        violations: list[str] = []
        for module_path in sorted(ORCHESTRATOR_ROOT.rglob("*.py")):
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in banned_names:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}")
        self.assertEqual(
            violations,
            [],
            msg=f"Reply transport compatibility shims should not have production references: {violations}",
        )


if __name__ == "__main__":
    unittest.main()
