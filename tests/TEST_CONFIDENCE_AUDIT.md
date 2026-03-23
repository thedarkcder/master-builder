# Test Confidence Audit

This repo now distinguishes between three test roles:

- `production_path`: real app/runtime orchestration with only external boundaries faked
- `contract`: adapter, wrapper, or seam-shape checks that do not prove full production behavior
- `smoke`: broad no-500/no-crash coverage only

## Current confidence suites

These are the highest-signal tests for user-facing and runtime behavior today:

- `tests/test_discord_commands_production_paths.py`
- `tests/test_discord_interactions_production_paths.py`
- `tests/test_agent_tool_cli_runtime.py`

These are the suites CI should treat as confidence for the currently hardened areas:

- Discord command endpoint behavior
- Discord interaction + deferred followup behavior
- Codex/agent-tool CLI runtime behavior
- Decision-engine stateful behavior
- Decision-planner behavior

## Reclassified wrapper-heavy suites

These files are useful, but they are not confidence-grade for end-to-end behavior:

- `tests/test_api_routes_webhooks_e2e_smoke.py` -> `smoke`
- `tests/test_webhook_discord_route.py` -> `contract`
- `tests/test_webhook_discord_interactions_routes.py` -> `contract`
- `tests/test_discord_thread_binding_flow.py` -> `contract`
- `tests/test_webhook_followup_service.py` -> `contract`
- `tests/test_discord_runtime_integrations.py` -> `contract`
- `tests/test_discord_commands.py` -> `contract`
- `tests/test_github_ingress.py` -> `contract`
- `tests/test_webhook.py` -> `contract`
- `tests/test_gateway_listener_runtime.py` -> `contract`
- `tests/test_api_error_observability.py` -> `contract`

Reasons these do not count as production confidence:

- patching top-level ingress/route helpers
- patching executor/dispatcher entrypoints directly
- closing deferred coroutines instead of awaiting them
- proving wrapper wiring rather than the real endpoint/runtime path

## Remaining subsystems without enough real confidence tests

These still need production-path replacements or additions:

- Jira webhook ingestion and decision/reply flows
- GitHub webhook ingress, remediation, and publication flows
- Live voice / voice ingress and followup flows
- Gateway listener behavior under real deferred task execution instead of closed tasks

## Confidence rule

When a change touches a user-facing command, webhook, followup, or external-service seam, at least one `production_path` test should cover the real internal path for that subsystem. `contract` and `smoke` tests are still useful, but they should not be treated as the primary release signal.
