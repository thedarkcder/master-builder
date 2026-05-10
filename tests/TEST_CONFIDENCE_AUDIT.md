# Test Confidence Audit

This repo now distinguishes between three test roles:

- `production_path`: real app/runtime orchestration with only external boundaries faked
- `contract`: adapter, wrapper, or seam-shape checks that do not prove full production behavior
- `smoke`: broad no-500/no-crash coverage only

For external ingress paths, `production_path` tests should use documented payload fixtures or sanitized live payload fixtures. Handcrafted minimal dict payloads are acceptable for narrow `contract` tests, but they are not confidence-grade for gateway, interaction, or webhook behavior.

## Current confidence suites

These are the highest-signal tests for user-facing and runtime behavior today:

- `tests/test_discord_commands_production_paths.py`
- `tests/test_discord_webhook_production_paths.py`
- `tests/test_discord_interactions_production_paths.py`
- `tests/test_agent_tool_cli_runtime.py`
- `tests/test_jira_webhook_production_paths.py`
- `tests/test_github_webhook_production_paths.py`
- `tests/test_gateway_listener_production_paths.py`
- `tests/test_live_voice_production_paths.py`

These are the suites CI should treat as confidence for the currently hardened areas:

- Discord command endpoint behavior
- Discord webhook deferred command behavior
- Discord interaction + deferred followup behavior
- Jira webhook ingestion and decision/reply behavior
- GitHub webhook ingress, remediation, and publication behavior
- Gateway listener deferred task behavior
- Live voice ingress and followup behavior
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
- proving wrapper wiring rather than the real endpoint/runtime path

Several of these suites have been cleaned up so they no longer `coro.close()` deferred work, but they are still contract tests because they patch the main orchestration seams instead of running the real behavior.

## Remaining subsystems without enough real confidence tests

These still need additional production-path depth beyond the new baseline:

- Jira webhook coverage for project-not-mapped skip and notification side effects
- GitHub remediation completion publication and broader review-trigger coverage
- Live voice transport-room event coverage beyond direct turn processing
- Gateway listener coverage for issue-bound reply and seed followup thread flows

## Confidence rule

When a change touches a user-facing command, webhook, followup, or external-service seam, at least one `production_path` test should cover the real internal path for that subsystem. `contract` and `smoke` tests are still useful, but they should not be treated as the primary release signal.

No command is considered covered unless there is at least one production-path test that runs the real route and the real command module without patching the command’s main orchestration function.

For external-service ingress, a command or webhook is not considered covered unless that production-path test uses a documented payload fixture or a sanitized live payload fixture for the ingress event under test.
