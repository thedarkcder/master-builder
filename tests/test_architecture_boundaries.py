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
from orchestrator.core.workflow_runtime import WorkflowAdvanceRequest


ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_ROOT = ROOT / "orchestrator"

# Explicit allowlist for intentionally permitted route coupling.
# The test fails on any new coupling or stale expectation drift.
LEGACY_ROUTE_IMPORT_ALLOWLIST = {
    "orchestrator/core/discord/gateway_runtime.py": set(),
    "orchestrator/api/routes/admin_tenants.py": {
        "orchestrator.api.routes.app_auth",
    },
    "orchestrator/api/routes/discord.py": set(),
    "orchestrator/api/routes/webhook.py": set(),
    "orchestrator/api/routes/webhook_discord_interactions.py": set(),
    "orchestrator/api/routes/webhook_github.py": set(),
}
NON_ROUTE_API_ROUTE_IMPORT_ALLOWLIST = {
    "orchestrator/api/main.py": {
        "orchestrator.api.routes.admin_auth",
        "orchestrator.api.routes.admin_agent_runtimes",
        "orchestrator.api.routes.admin_architecture_documents",
        "orchestrator.api.routes.admin_atlassian_confluence",
        "orchestrator.api.routes.admin_atlassian_jira",
        "orchestrator.api.routes.admin_atlassian_oauth",
        "orchestrator.api.routes.admin_atlassian_webhooks",
        "orchestrator.api.routes.admin_codex",
        "orchestrator.api.routes.admin_discord_commands",
        "orchestrator.api.routes.admin_discord_allowlist",
        "orchestrator.api.routes.admin_discord_install",
        "orchestrator.api.routes.admin_github",
        "orchestrator.api.routes.admin_knowledge",
        "orchestrator.api.routes.admin_observability",
        "orchestrator.api.routes.admin_ready",
        "orchestrator.api.routes.admin_release",
        "orchestrator.api.routes.admin_runs",
        "orchestrator.api.routes.admin_secrets",
        "orchestrator.api.routes.admin_tenants",
        "orchestrator.api.routes.admin_tokens",
        "orchestrator.api.routes.app_auth",
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
    def test_abstract_runtime_modules_do_not_use_vendor_runtime_function_names(self) -> None:
        banned_token = "with_" + "codex"
        roots = [
            ORCHESTRATOR_ROOT / "core",
            ORCHESTRATOR_ROOT / "api",
        ]
        violations: list[str] = []
        for root in roots:
            for module_path in sorted(root.rglob("*.py")):
                source = module_path.read_text(encoding="utf-8")
                if banned_token in source:
                    violations.append(module_path.relative_to(ROOT).as_posix())

        self.assertEqual(
            violations,
            [],
            msg=f"Runtime-abstracted modules must use domain/runtime names, not vendor names: {violations}",
        )

    def test_specialist_planning_does_not_restore_pm_internal_resolution_contract(self) -> None:
        banned_tokens = {
            "pm_" + "internal_resolution",
            "internal_" + "decisions",
            "open_" + "behavior_questions",
            "stakeholder_" + "escalation_recommendations",
        }
        roots = [
            ORCHESTRATOR_ROOT / "core",
            ORCHESTRATOR_ROOT / "prompts",
        ]
        violations: list[str] = []
        for root in roots:
            for module_path in sorted(root.rglob("*")):
                if module_path.suffix not in {".py", ".j2"}:
                    continue
                source = module_path.read_text(encoding="utf-8")
                for token in banned_tokens:
                    if token in source:
                        violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{token}")

        self.assertEqual(
            violations,
            [],
            msg=f"Specialist planning must use technical_decisions/product_escalations only: {violations}",
        )

    def test_atlassian_admin_surface_is_split_by_provider_capability(self) -> None:
        stale_module = ORCHESTRATOR_ROOT / "api" / "routes" / "admin_atlassian.py"
        self.assertFalse(
            stale_module.exists(),
            msg="Do not restore the broad Atlassian admin router; split OAuth, Jira, Confluence, and webhook routes.",
        )
        jira_route = ORCHESTRATOR_ROOT / "api" / "routes" / "admin_atlassian_jira.py"
        jira_source = jira_route.read_text(encoding="utf-8")
        self.assertNotIn(
            "webhooks/",
            jira_source,
            msg="Jira project discovery routes must not own Jira webhook lifecycle endpoints.",
        )
        webhook_route = ORCHESTRATOR_ROOT / "api" / "routes" / "admin_atlassian_webhooks.py"
        self.assertTrue(webhook_route.exists(), msg="Jira webhook lifecycle must have an explicit Atlassian webhook route module.")

    def test_jira_http_ingress_does_not_plan_issue_runs_inline(self) -> None:
        module_path = ROOT / "orchestrator" / "api" / "webhooks" / "jira_application.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

        target_function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "build_jira_webhook_ingress_result"
        )

        violations: list[str] = []
        saw_enqueue_call = False
        for node in ast.walk(target_function):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "enqueue_webhook_job":
                saw_enqueue_call = True
            if isinstance(func, ast.Name) and func.id in {"plan_jira_run_flow", "evaluate_jira_trigger_state"}:
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:{func.id}")

        self.assertTrue(saw_enqueue_call, msg="Jira HTTP ingress must enqueue issue events for worker reconciliation.")
        self.assertEqual(
            violations,
            [],
            msg="Jira HTTP ingress must not evaluate or plan issue runs inline.",
        )

    def test_github_http_ingress_does_not_plan_reviews_inline(self) -> None:
        module_path = ROOT / "orchestrator" / "api" / "webhooks" / "github_ingress.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

        target_function = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "ingest_github_webhook_event"
        )

        violations: list[str] = []
        saw_enqueue_call = False
        for node in ast.walk(target_function):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "enqueue_webhook_job":
                saw_enqueue_call = True
            if isinstance(func, ast.Name) and func.id in {
                "build_github_webhook_ingress_result",
                "plan_pull_request_targets",
                "prepare_github_webhook_runtime",
            }:
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:{func.id}")

        self.assertTrue(saw_enqueue_call, msg="GitHub HTTP ingress must enqueue actionable events.")
        self.assertEqual(
            violations,
            [],
            msg="GitHub HTTP ingress must not plan reviews or build runtime inline.",
        )

    def test_discord_webhook_routes_do_not_spawn_route_level_tasks(self) -> None:
        modules = [
            ROOT / "orchestrator" / "api" / "routes" / "webhook_discord.py",
            ROOT / "orchestrator" / "api" / "routes" / "webhook_discord_interactions.py",
        ]
        violations: list[str] = []
        saw_enqueue = {module.name: False for module in modules}
        for module_path in modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "create_task":
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:create_task")
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "enqueue_webhook_job":
                    saw_enqueue[module_path.name] = True

        self.assertEqual(
            violations,
            [],
            msg="Webhook routes must not spawn ad hoc route-level tasks once queueing is in place.",
        )
        self.assertTrue(
            all(saw_enqueue.values()),
            msg=f"Webhook routes must enqueue work instead of spawning tasks: {saw_enqueue}",
        )

    def test_run_controls_do_not_directly_reopen_decision_gate(self) -> None:
        module_path = ROOT / "orchestrator" / "api" / "discord" / "commands" / "run_controls.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

        violations: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "orchestrator.core.decision_clarification_service"
                and any(alias.name == "evaluate_issue_clarification_state" for alias in node.names)
            ):
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}")

        self.assertEqual(
            violations,
            [],
            msg="Run controls must not evaluate Decision Gate directly; board-ingress owns clarification.",
        )

    def test_worker_decision_gate_does_not_use_runtime_decision_engine(self) -> None:
        module_path = ROOT / "orchestrator" / "core" / "worker" / "decision_gate.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

        violations: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "orchestrator.core.decision_engine"
                and any(alias.name == "evaluate_worker_decision" for alias in node.names)
            ):
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id == "evaluate_worker_decision":
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}")

        self.assertEqual(
            violations,
            [],
            msg="Worker start gating must remain readiness-only and not re-evaluate Decision Gate.",
        )

    def test_run_lifecycle_does_not_use_tenant_row_as_claim_mutex(self) -> None:
        module_path = ROOT / "orchestrator" / "core" / "worker" / "run_lifecycle.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))

        violations: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "orchestrator.storage.models"
                and any(alias.name == "Tenant" for alias in node.names)
            ):
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:Tenant import")
            if isinstance(node, ast.FunctionDef) and node.name == "_lock_tenant_row_for_claim":
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:tenant claim helper")
            if isinstance(node, ast.Name) and node.id == "_lock_tenant_row_for_claim":
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:tenant claim reference")

        self.assertEqual(
            violations,
            [],
            msg="Worker run claim must not lock the business tenants table.",
        )

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

    def test_transport_and_worker_adapters_do_not_call_enqueue_reason_guidance_directly(self) -> None:
        roots = [
            ROOT / "orchestrator" / "api" / "webhooks",
            ROOT / "orchestrator" / "api" / "discord" / "commands",
            ROOT / "orchestrator" / "core" / "worker",
        ]
        violations: list[str] = []
        for root in roots:
            for module_path in sorted(root.rglob("*.py")):
                tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    if isinstance(func, ast.Name) and func.id == "enqueue_reason_guidance":
                        violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}")
                    if isinstance(func, ast.Attribute) and func.attr == "enqueue_reason_guidance":
                        violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}")
        self.assertEqual(
            violations,
            [],
            msg=f"Direct enqueue_reason_guidance calls found in transport/worker adapters: {violations}",
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

    def test_workflow_infrastructure_does_not_import_parent_feature_handlers_directly(self) -> None:
        modules = [
            ROOT / "orchestrator" / "api" / "admin" / "workflows_service.py",
            ROOT / "orchestrator" / "temporal" / "activities" / "handler_workflow.py",
            ROOT / "orchestrator" / "core" / "workflow_runtime.py",
            ROOT / "orchestrator" / "core" / "workflow_handler_registry.py",
            ROOT / "orchestrator" / "core" / "workflow_engine.py",
            ROOT / "orchestrator" / "core" / "workflow_engine_factory.py",
            ROOT / "orchestrator" / "core" / "legacy_workflow_engine.py",
            ROOT / "orchestrator" / "temporal" / "workflow_engine.py",
        ]
        banned_imports = {
            "orchestrator.core.parent_feature_workflow.handlers",
            "orchestrator.core.parent_feature_workflow.retry",
        }
        violations: list[str] = []
        for module_path in modules:
            for module_name in _imported_modules(module_path):
                if module_name in banned_imports:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Workflow infrastructure importing parent-feature handlers directly: {violations}",
        )

    def test_admin_workflows_service_is_route_facade_not_god_service(self) -> None:
        module_path = ROOT / "orchestrator" / "api" / "admin" / "workflows_service.py"
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        local_functions = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
        banned_local_helpers = {
            "_workflow_schema",
            "_workflow_operation_reads",
            "_workflow_links",
            "_workflow_runs",
            "_workflow_operations",
            "_pending_input_request",
            "_step_buckets",
            "_fresh_start_plan",
            "_checkpoint_resume_plan",
            "_cursor_filtered_events",
        }
        violations = sorted(banned_local_helpers.intersection(local_functions))
        self.assertLessEqual(
            len(local_functions),
            6,
            msg=f"Admin workflows service must remain a route-facing facade; found functions: {local_functions}",
        )
        self.assertEqual(
            violations,
            [],
            msg=f"Admin workflows service must not own implementation helpers: {violations}",
        )

    def test_temporal_handler_activity_does_not_import_workflow_provider_adapters(self) -> None:
        module_path = ROOT / "orchestrator" / "temporal" / "activities" / "handler_workflow.py"
        banned_prefixes = {
            "orchestrator.api.atlassian_oauth",
            "orchestrator.api.discord",
            "orchestrator.api.webhooks",
            "orchestrator.core.parent_feature_workflow",
        }
        violations: list[str] = []
        for module_name in _imported_modules(module_path):
            if any(module_name.startswith(prefix) for prefix in banned_prefixes):
                violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Temporal workflow activities must dispatch through the installed workflow registry, not provider adapters: {violations}",
        )

    def test_workflow_advance_contract_is_provider_agnostic(self) -> None:
        field_names = {field.name for field in fields(WorkflowAdvanceRequest)}
        banned_fields = {
            "issue_key",
            "issue_summary",
            "issue_description",
            "issue_labels",
            "webhook_event",
            "comment_command",
            "comment_command_argument",
        }
        self.assertFalse(
            field_names.intersection(banned_fields),
            msg=f"Workflow advance contract must not expose provider-specific fields: {field_names}",
        )

    def test_workflow_runtime_and_engines_do_not_use_retry_callback_escape_hatch(self) -> None:
        modules = [
            ROOT / "orchestrator" / "core" / "workflow_runtime.py",
            ROOT / "orchestrator" / "core" / "workflow_engine.py",
            ROOT / "orchestrator" / "core" / "workflow_engine_factory.py",
            ROOT / "orchestrator" / "core" / "legacy_workflow_engine.py",
            ROOT / "orchestrator" / "temporal" / "workflow_engine.py",
        ]
        banned_tokens = {"retry_" + "workflow_operation_fn", "resolve_" + "operation_retry_handler_fn"}
        violations = []
        for module_path in modules:
            source = module_path.read_text(encoding="utf-8")
            for banned_token in banned_tokens:
                if banned_token in source:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{banned_token}")
        self.assertEqual(
            violations,
            [],
            msg=f"Workflow retry callback escape hatch found: {violations}",
        )

    def test_parent_feature_workflow_capabilities_are_separate(self) -> None:
        module_paths = [
            ROOT / "orchestrator" / "core" / "parent_feature_workflow" / "handlers.py",
            ROOT / "orchestrator" / "core" / "parent_feature_workflow" / "retry.py",
        ]
        class_methods: dict[str, set[str]] = {}
        class_modules: dict[str, str] = {}
        for module_path in module_paths:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name in {
                    "ParentFeatureWorkflowAdvanceHandler",
                    "ParentFeatureWorkflowOperationRetryHandler",
                }:
                    class_methods[node.name] = {
                        item.name
                        for item in node.body
                        if isinstance(item, ast.FunctionDef)
                    }
                    class_modules[node.name] = module_path.name
        self.assertEqual(class_modules.get("ParentFeatureWorkflowAdvanceHandler"), "handlers.py")
        self.assertEqual(class_modules.get("ParentFeatureWorkflowOperationRetryHandler"), "retry.py")
        self.assertNotIn(
            "retry_operation",
            class_methods.get("ParentFeatureWorkflowAdvanceHandler", set()),
            msg="Parent feature advance handler must not own operation retry.",
        )
        self.assertNotIn(
            "advance",
            class_methods.get("ParentFeatureWorkflowOperationRetryHandler", set()),
            msg="Parent feature retry handler must not own advance routing.",
        )

    def test_parent_operation_names_do_not_leak_into_workflow_infrastructure(self) -> None:
        modules = [
            ROOT / "orchestrator" / "api" / "admin" / "workflows_service.py",
            ROOT / "orchestrator" / "temporal" / "activities" / "handler_workflow.py",
            ROOT / "orchestrator" / "core" / "workflow_runtime.py",
            ROOT / "orchestrator" / "core" / "workflow_advance.py",
            ROOT / "orchestrator" / "core" / "workflow_engine.py",
            ROOT / "orchestrator" / "core" / "workflow_engine_factory.py",
            ROOT / "orchestrator" / "core" / "legacy_workflow_engine.py",
            ROOT / "orchestrator" / "temporal" / "workflow_engine.py",
        ]
        banned_values = {"backlog_planning", "jira_comment_projection", "jira_child_fanout", "jira_parent_update"}
        violations: list[str] = []
        for module_path in modules:
            source = module_path.read_text(encoding="utf-8")
            for value in banned_values:
                if value in source:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{value}")
        self.assertEqual(
            violations,
            [],
            msg=f"Parent operation names leaked into workflow infrastructure: {violations}",
        )

    def test_issue_fanout_is_not_owned_by_discord_ingress_modules(self) -> None:
        stale_modules = [
            ROOT / "orchestrator" / "api" / "discord" / "ingress" / "seed_runtime.py",
            ROOT / "orchestrator" / "api" / "discord" / "seed" / "issue_service.py",
            ROOT / "orchestrator" / "api" / "discord" / "seed" / "draft_assembly.py",
            ROOT / "orchestrator" / "api" / "discord" / "seed" / "description.py",
            ROOT / "orchestrator" / "api" / "discord" / "seed" / "matching.py",
        ]
        self.assertEqual(
            [path.relative_to(ROOT).as_posix() for path in stale_modules if path.exists()],
            [],
            msg="Issue fanout must live in core/runtime, not Discord ingress/seed modules.",
        )
        forbidden_prefixes = {
            "orchestrator.api.discord.ingress.seed_runtime",
            "orchestrator.api.discord.seed.issue_service",
            "orchestrator.api.discord.seed.draft_assembly",
            "orchestrator.api.discord.seed.description",
            "orchestrator.api.discord.seed.matching",
        }
        violations: list[str] = []
        for module_path in sorted(ORCHESTRATOR_ROOT.rglob("*.py")):
            for module_name in _imported_modules(module_path):
                if module_name in forbidden_prefixes:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Discord-owned issue fanout imports found: {violations}",
        )

    def test_product_event_streams_use_shared_stream_primitive(self) -> None:
        stream_modules = [
            ROOT / "orchestrator" / "api" / "admin" / "workflow_live_stream_service.py",
            ROOT / "orchestrator" / "api" / "admin" / "run_logging_stream_service.py",
            ROOT / "orchestrator" / "api" / "admin" / "runtime_logs_service.py",
        ]
        for module_path in stream_modules:
            source = module_path.read_text(encoding="utf-8")
            self.assertIn("stream_product_event_rows", source)
            self.assertNotIn("wait_for_product_event_notification", source)
            self.assertNotIn("current_product_event_notification_marker", source)

    def test_product_events_facade_does_not_own_storage_writer_or_streaming(self) -> None:
        product_events_source = (ROOT / "orchestrator" / "core" / "product_events.py").read_text(encoding="utf-8")
        forbidden_tokens = [
            "urlopen",
            "Request(",
            "_insert_sql",
            "_where_clause",
            "publish_product_event_notification",
            "wait_for_product_event_notification",
            "current_product_event_notification_marker",
            "WorkflowOperationAttempt",
        ]
        violations = [token for token in forbidden_tokens if token in product_events_source]
        self.assertEqual(
            violations,
            [],
            msg=f"product_events.py must remain a thin facade over repository/writer/stream services: {violations}",
        )
        repository_source = (ROOT / "orchestrator" / "core" / "product_event_repository.py").read_text(encoding="utf-8")
        writer_source = (ROOT / "orchestrator" / "core" / "product_event_writer.py").read_text(encoding="utf-8")
        stream_source = (ROOT / "orchestrator" / "core" / "product_event_stream.py").read_text(encoding="utf-8")
        self.assertIn("class ProductEventRepository", repository_source)
        self.assertIn("class ClickHouseProductEventRepository", repository_source)
        self.assertIn("class ProductEventWriter", writer_source)
        self.assertIn("class ProductEventStream", stream_source)

    def test_admin_runs_route_is_not_the_composition_root(self) -> None:
        source = (ROOT / "orchestrator" / "api" / "routes" / "admin_runs.py").read_text(encoding="utf-8")
        forbidden_imports = [
            "orchestrator.api.admin.runtime_logs_service",
            "orchestrator.api.admin.workflow_live_stream_service",
            "orchestrator.api.admin.run_logging_stream_service",
            "orchestrator.api.admin.runs_query",
            "orchestrator.api.admin.runs_service",
            "orchestrator.api.admin.schema_mappers",
            "orchestrator.api.admin.workflows_service",
            "orchestrator.runtime.issue_fanout",
            "orchestrator.core.agent_runtime_resolver",
            "orchestrator.core.jira_links",
            "orchestrator.core.workflow_integration_router",
            "orchestrator.storage.models",
            "select(",
        ]
        violations = [token for token in forbidden_imports if token in source]
        self.assertEqual(
            violations,
            [],
            msg=f"admin_runs.py must validate web concerns and delegate to admin use cases, not compose concrete services: {violations}",
        )
        self.assertIn("runs_workflows_use_cases", source)

    def test_run_human_input_resume_does_not_use_private_legacy_entrypoint(self) -> None:
        banned_token = "_resume_workflow_from_human_input_answer_legacy"
        violations = []
        for module_path in sorted(ORCHESTRATOR_ROOT.rglob("*.py")):
            if banned_token in module_path.read_text(encoding="utf-8"):
                violations.append(module_path.relative_to(ROOT).as_posix())
        self.assertEqual(
            violations,
            [],
            msg=f"Human-input resume must use explicit use-case entrypoints, not private legacy symbols: {violations}",
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

    def test_gateway_and_followup_runtime_modules_do_not_construct_provider_executors_directly(self) -> None:
        modules = [
            ROOT / "orchestrator" / "core" / "discord" / "gateway_listener.py",
            ROOT / "orchestrator" / "api" / "discord" / "interactions" / "followup_runtime.py",
        ]
        violations: list[str] = []
        for module_path in modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Name) and func.id in {"DiscordTransportExecutor", "GitHubTransportExecutor"}:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:{func.id}")
        self.assertEqual(
            violations,
            [],
            msg=f"Gateway/followup runtime should not construct provider executors directly: {violations}",
        )

    def test_github_application_does_not_use_callback_executor_registration(self) -> None:
        module_path = ROOT / "orchestrator" / "api" / "webhooks" / "github_application.py"
        source = module_path.read_text(encoding="utf-8")
        self.assertNotIn("register_transport_executor", source)

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

    def test_workflow_core_modules_do_not_import_api_layer(self) -> None:
        violations: list[str] = []
        for module_path in sorted((ORCHESTRATOR_ROOT / "core" / "workflow").rglob("*.py")):
            for module_name in _imported_modules(module_path):
                if module_name.startswith("orchestrator.api"):
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Workflow core modules must not import API-layer modules: {violations}",
        )

    def test_transport_modules_do_not_branch_on_decision_classification_or_reason_strings(self) -> None:
        modules = [
            ROOT / "orchestrator" / "api" / "discord" / "commands" / "run_controls.py",
            ROOT / "orchestrator" / "api" / "webhooks" / "jira_admission_flow.py",
            ROOT / "orchestrator" / "api" / "webhooks" / "jira_webhook_comment_flow.py",
        ]
        banned_values = {
            "decision_gate",
            "gtd",
            "both",
            "clear",
            "decision_gate_required",
            "gtd_required",
            "missing_ready_label",
            "policy_eval_failed",
        }
        banned_names = {"classification", "reason_code", "block_reason"}
        violations: list[str] = []
        for module_path in modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Compare):
                    compare_targets = [node.left, *node.comparators]
                    compare_names = {
                        subnode.id
                        for target in compare_targets
                        for subnode in ast.walk(target)
                        if isinstance(subnode, ast.Name)
                    }
                    compare_attrs = {
                        subnode.attr
                        for target in compare_targets
                        for subnode in ast.walk(target)
                        if isinstance(subnode, ast.Attribute)
                    }
                    if not (banned_names & (compare_names | compare_attrs)):
                        continue
                    constants = {
                        value_node.value
                        for target in compare_targets
                        for value_node in ast.walk(target)
                        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str)
                    }
                    if constants & banned_values:
                        violations.append(
                            f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:{sorted(constants & banned_values)!r}"
                        )
        self.assertEqual(
            violations,
            [],
            msg=f"Transport modules must consume typed domain outcomes instead of branching on decision strings: {violations}",
        )

    def test_transport_modules_do_not_import_split_decision_runtime_modules(self) -> None:
        modules = [
            ROOT / "orchestrator" / "api" / "webhooks" / "jira_admission_flow.py",
            ROOT / "orchestrator" / "api" / "webhooks" / "jira_webhook_comment_flow.py",
            ROOT / "orchestrator" / "api" / "discord" / "commands" / "run_controls.py",
            ROOT / "orchestrator" / "core" / "worker" / "run_not_ready.py",
        ]
        forbidden_imports = {
            "orchestrator.core.execution_admission",
            "orchestrator.core.decision_state_reducer",
        }
        violations: list[str] = []
        for module_path in modules:
            for module_name in _imported_modules(module_path):
                if module_name in forbidden_imports:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Transport modules must import unified decision_state_machine boundary only: {violations}",
        )

    def test_decision_transition_logic_lives_in_decision_state_machine_boundary(self) -> None:
        state_machine_module = ROOT / "orchestrator" / "core" / "decision_state_machine.py"
        self.assertTrue(state_machine_module.exists(), msg="decision_state_machine.py must exist as the canonical transition boundary")

        forbidden_modules = [
            ROOT / "orchestrator" / "core" / "decision_precheck_mapping.py",
            ROOT / "orchestrator" / "core" / "decision_state_repository.py",
        ]
        forbidden_symbols = {
            "DecisionStateTransition",
            "DecisionState",
            "resolve_decision_state_transition",
            "resolve_readiness_decision",
            "resolve_execution_gate_state",
            "resolve_execution_admission",
            "case_state_for_decision",
            "decision_reason",
            "resolve_worker_decision_from_precheck",
            "resolve_worker_blocked_outcome",
        }
        violations: list[str] = []
        for module_path in forbidden_modules:
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in forbidden_symbols:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:{node.name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Canonical decision/readiness semantics must only live in decision_state_machine: {violations}",
        )

    def test_decision_engine_uses_state_machine_boundary_for_transition_semantics(self) -> None:
        module_path = ROOT / "orchestrator" / "core" / "decision_engine.py"
        imports = _imported_modules(module_path)
        self.assertIn(
            "orchestrator.core.decision_state_machine",
            imports,
            msg="decision_engine must import transition semantics from decision_state_machine",
        )

        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        violations: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module != "orchestrator.core.decision_state_reducer":
                if node.module == "orchestrator.core.decision_precheck_mapping":
                    for alias in node.names:
                        if alias.name == "decision_from_snapshot":
                            violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:{alias.name}")
                continue
            for alias in node.names:
                if alias.name in {"DecisionStateTransition", "DecisionStateReducerInput", "reduce_decision_state_transition"}:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{node.lineno}:{alias.name}")
        self.assertEqual(
            violations,
            [],
            msg=f"decision_engine must consume canonical state-machine semantics directly: {violations}",
        )

    def test_core_decision_runtime_modules_do_not_import_compat_wrappers(self) -> None:
        modules = [
            ROOT / "orchestrator" / "core" / "decision_engine.py",
            ROOT / "orchestrator" / "core" / "decision_precheck_mapping.py",
            ROOT / "orchestrator" / "core" / "decision_state_repository.py",
        ]
        forbidden_imports = {
            "orchestrator.core.decision_reducer",
            "orchestrator.core.precheck_decision",
        }
        violations: list[str] = []
        for module_path in modules:
            for module_name in _imported_modules(module_path):
                if module_name in forbidden_imports:
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=f"Core decision runtime modules must use canonical decision_state_machine helpers directly: {violations}",
        )

    def test_removed_decision_compatibility_facades_do_not_return(self) -> None:
        removed_modules = [
            ROOT / "orchestrator" / "core" / "decision_reducer.py",
            ROOT / "orchestrator" / "core" / "decision_state_reducer.py",
            ROOT / "orchestrator" / "core" / "precheck_decision.py",
        ]
        violations = [module_path.relative_to(ROOT).as_posix() for module_path in removed_modules if module_path.exists()]
        self.assertEqual(
            violations,
            [],
            msg=f"Decision compatibility facades should be removed once callers are migrated: {violations}",
        )

    def test_api_and_worker_bootstrap_execution_snapshot_startup_migration(self) -> None:
        api_path = ROOT / "orchestrator" / "api" / "main.py"
        worker_path = ROOT / "orchestrator" / "worker.py"
        expected_module = "orchestrator.core.workflow.execution_snapshot_startup"
        expected_symbol = "ensure_execution_snapshot_startup_bootstrap"

        for module_path in (api_path, worker_path):
            tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
            imports_ok = False
            call_ok = False
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == expected_module:
                    imports_ok = any(alias.name == expected_symbol for alias in node.names)
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id == expected_symbol:
                        call_ok = True
            self.assertTrue(
                imports_ok,
                msg=f"{module_path.relative_to(ROOT).as_posix()} must import shared execution snapshot startup bootstrap.",
            )
            self.assertTrue(
                call_ok,
                msg=f"{module_path.relative_to(ROOT).as_posix()} must invoke shared execution snapshot startup bootstrap.",
            )

    def test_legacy_execution_snapshot_migration_module_is_removed(self) -> None:
        legacy_module = ROOT / "orchestrator" / "core" / "workflow" / "execution_snapshot_migration.py"
        self.assertFalse(
            legacy_module.exists(),
            msg="Legacy execution snapshot runtime conversion module should be removed after cutover.",
        )

    def test_legacy_decision_snapshot_codec_module_is_removed(self) -> None:
        legacy_module = ROOT / "orchestrator" / "core" / "decision_snapshot_codec.py"
        self.assertFalse(
            legacy_module.exists(),
            msg="Legacy decision_snapshot_codec module should be removed after state-machine cutover.",
        )

    def test_runtime_modules_do_not_import_decision_snapshot_codec_directly(self) -> None:
        violations: list[str] = []
        for module_path in sorted(ORCHESTRATOR_ROOT.rglob("*.py")):
            for module_name in _imported_modules(module_path):
                if module_name == "orchestrator.core.decision_snapshot_codec":
                    violations.append(f"{module_path.relative_to(ROOT).as_posix()}:{module_name}")
        self.assertEqual(
            violations,
            [],
            msg=(
                "Decision snapshot helpers must flow through the canonical decision_state_machine boundary: "
                f"{violations}"
            ),
        )


if __name__ == "__main__":
    unittest.main()
