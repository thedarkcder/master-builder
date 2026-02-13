# Error Swallow Inventory

This file tracks current non-fatal/defensive catch-and-continue paths for follow-up in Jira.

## Remediated in current branch

- `orchestrator/api/discord/bug/attachments.py`
  - Attachment upload path now returns structured `AttachmentUploadFailure` objects and propagates as a hard error from `create_discord_bug_issue` instead of continuing.
  - `orchestrator/api/discord/bug/issue_create_service.py` now returns `502` if any attachment fetch/upload fails.
- `orchestrator/core/secret_manager.py`
  - Scope-boundary ambiguity removed by explicit tenant/project/platform resolution paths.
  - Tenant/project callers no longer implicitly fall back to platform or unscoped env values.
- `orchestrator/api/routes/admin_secrets.py`
  - Secret resolution/upsert/list/delete now use platform and tenant secret services explicitly.
  - Removed endpoint-level `_normalize_platform_secret_ref` mapping.

## Remaining non-fatal fallbacks (active)

### High Priority (User-facing correctness / data integrity)
- `orchestrator/core/secret_manager.py`
  - `resolve_scoped_secret_ref`: for unscoped lookups without tenant/project context, this can still resolve unscoped env vars.
  - Impact: tenant callers should pass tenant context and avoid env-based cross-domain fallback where explicit scoping is required.
  - Jira follow-up: `MAB-150`

### Medium Priority (Operational / observability)
- `orchestrator/core/platform_metrics.py`
  - Metrics reads/writes log and continue on failures.
  - Jira follow-up: `MAB-154`
- `orchestrator/core/error_observability.py`
  - Best-effort Sentry capture path with broad exception swallow.
  - Jira follow-up: `MAB-154`

- `orchestrator/api/webhooks/followup_service.py:398`, `418`, `445`
  - Follow-up fallback send paths catch broad exceptions and only log.
  - Impact: user follow-up visibility can be partial during Discord API/runtime issues.
  - Jira follow-up: `MAB-151`

- `orchestrator/core/discord/gateway_listener.py:91`, `111`, `207`, `306`
  - Gateway loop catches broad exceptions and continues/retries.
  - Impact: command handling may continue in a degraded loop without failing hard.
  - Jira follow-up: `MAB-152`
- `orchestrator/core/worker/queue_listener.py:47`, `68`
  - Queue listener lifecycle errors are logged and loop continues.
  - Jira follow-up: `MAB-153`

### Low Priority / defensive
- `orchestrator/api/webhooks/followup_service.py:181`, `221`, `276`, `304`, `398`
  - Explicit defensive fallback paths for expired/callback failures.
  - Jira follow-up: `MAB-151`
- `orchestrator/core/discord/gateway_listener.py:296`
  - Command/API exception handling during websocket maintenance.
  - Jira follow-up: `MAB-152`
- `orchestrator/api/discord/bug/attachments.py:78`
  - `resolve_discord_channel_name` treats Discord channel lookup failures as non-fatal and continues without channel metadata.
  - Jira follow-up: `MAB-155`

## Candidate ticket pattern for Jira
For each item above:
1. Decide whether the current behavior should remain best-effort or become hard-fail.
2. If hard-fail is desired, define rollback, retry strategy, and user-facing status.
3. Add structured log fields for service, tenant, correlation, and operation scope.
