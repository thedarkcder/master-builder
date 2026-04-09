# Staff Engineer Review: Temporal Spike Plan

## 1. Problem

The current custom orchestrator is carrying durable workflow responsibilities that are brittle at human-input, retry, and resume boundaries, and a bad migration plan would either preserve those weaknesses or cause an unnecessary rewrite.

## 2. Type

Workflow / long-running process

## 3. Invariants

- A paused run must resume at most once for a given answered human-input event.
- Orchestration state must survive worker restarts and deploys for the migrated path.
- External side effects such as Discord messages, PR mutations, and Jira updates must remain idempotent under retries and replayed events.
- Non-migrated paths must continue to run on the legacy orchestrator without behavior drift.
- Tenant and project boundaries must remain intact when starting, signaling, querying, or projecting workflow state.

## 4. Assumptions

- The first spike only needs one representative path, not every workflow shape, because that keeps the change reversible and isolates risk.
- Existing stage services are sufficiently modular to be wrapped as Activities without rewriting business logic; this is safe because their internals already exist and the spike can expose integration gaps early.
- Existing database tables can temporarily remain read models for admin/API compatibility; this is safe because the plan does not require them to stay authoritative for migrated orchestration state.
- Local development can tolerate a Temporal runtime dependency because the spike is explicitly for trial adoption, not a zero-infra design.

## 5. Contract matrix

| Input bucket | Before | After | Expected behavior |
| --- | --- | --- | --- |
| New legacy-routed run | Custom queue + worker path owns orchestration | Unchanged | Intentional no-op for non-migrated paths |
| New Temporal-routed run | Not supported | Temporal workflow starts once, projects status to DB | Intentional change |
| Human-input answer for active Temporal workflow | DB row consumed, resume run enqueued, duplicate window exists | Signal or Update applied once, workflow unblocks, duplicate answer deduped | Intentional change |
| Duplicate or replayed answer event | Can enqueue duplicate resume attempt | Ignored or idempotently acknowledged | Intentional change |
| Invalid workflow ID / unauthorized signal | May fail in app-specific paths | Rejected before signal side effects | Intentional change |
| Worker restart during pause | Relies on custom checkpoints + request rows | Temporal persists wait state in event history | Intentional change |

## 6. Call-path impact scan

- Admin/API run creation paths currently enqueue runs and rely on the worker loop; under the plan, selected entrypoints will route to either legacy enqueue or Temporal start based on backend selection.
- Discord reply or human-input answer handling currently creates resume attempts in application code; under the plan, those handlers translate the event into a Temporal Signal or Update for migrated workflows.
- Webhook ingestion paths may eventually signal running workflows; in the spike they should remain scoped to the selected path only.
- Admin observability paths that read run/workflow status will continue reading projected database state, now updated by Temporal-aware projection activities for migrated workflows.

## 7. Domain term contracts

- `Run`: canonical meaning is a user-visible execution attempt tied to a tenant/project issue context. Proof: existing DB and API surfaces already expose it as the operational unit.
- `WorkflowExecution`: canonical meaning is orchestration state across one logical run lifecycle. In the migrated path, Temporal workflow execution becomes the authoritative implementation of that concept.
- `waiting_for_input`: canonical meaning is a durable blocked state awaiting an external human answer, not merely a UI label. Proof: the current system already treats it as an active lifecycle state with resume semantics.
- `resume`: canonical meaning is a single transition from blocked-on-input to runnable work, not “enqueue another attempt whenever an answer is seen.” The plan corrects current semantic drift here.

## 8. Authorization & data-access contract

- Acting principals include admin users, webhook senders, and Discord responders mediated by server-side handlers.
- Start/signal/query operations must validate tenant/project/run ownership before touching Temporal workflow IDs.
- Temporal payloads must carry tenant ID, project ID, run ID, and request ID so projection and external-event handling can enforce boundaries.
- Projected read models must avoid widening visibility; admin/API queries should still filter by the same tenant/user access rules that exist today.

## 9. Lifecycle & state matrix

| Entity | States | Included in spike | Notes |
| --- | --- | --- | --- |
| Legacy run | queued, running, waiting_for_input, blocked, completed, failed | Yes | Remains active for non-migrated paths |
| Temporal workflow | running, waiting_for_signal, completing, completed, failed, cancelled | Yes | New authoritative orchestration state for migrated path |
| Human-input request | requested, answered, consumed/closed | Yes | Consumed semantics move to signal handling and idempotency rules |
| Projection row | pending sync, synced, stale/error | Yes | Needed for admin/API compatibility |

## 10. Proposed design

- Add a new orchestration layer under `orchestrator/temporal/**` and keep existing business logic under `orchestrator/core/**`.
- Use one Temporal workflow per run, with one workflow type for phase 1 and one workflow type for the first full run path in phase 2.
- Wrap existing stage execution and side-effect services as Activities.
- Introduce an orchestration backend router so entrypoints choose `legacy` or `temporal` once, rather than branching throughout the codebase.
- Project Temporal state back into existing database tables for observability and compatibility during migration.
- Use Signals or Updates for human-input answers and other external events.

## 11. Patterns used

- Workflow orchestration via Temporal because the problem is durable, long-running coordination with pauses and retries.
- Adapter pattern for wrapping existing services as Activities because it minimizes rewrite scope.
- Backend routing/strangler pattern because it allows a reversible migration with narrow cutover.
- Read-model projection because existing UI/API surfaces already depend on database-backed status views.

Alternatives rejected:

- Full rewrite of domain and orchestration together was rejected because it increases migration risk without solving the core orchestration problem faster.
- Direct in-place replacement of the legacy worker for all paths was rejected because rollback would be too coarse.

## 12. Patterns not used

- Child workflows are intentionally not used in the spike because the first problem is durability and external-event coordination, not workflow decomposition.
- Event sourcing in the application database is not introduced because Temporal event history already provides the durable orchestration log for migrated paths.
- Dual-authority orchestration state across DB and Temporal is not used because it creates reconciliation ambiguity.

## 13. Change surface

- New modules under `orchestrator/temporal/**`
- Routing changes in run-start and human-input/webhook entrypoints
- Projection hooks into existing persistence models
- Dependency additions in `pyproject.toml`
- Local/dev runtime changes in `docker-compose.yml` or equivalent scripts
- Tests for workflow start, signal handling, projection, retry behavior, and backend routing

## 14. Load shape & query plan

- Expected QPS is low to moderate for run starts and very low for human-input resume, but bursts can occur from webhook replay or batch admin actions.
- Temporal start/signal operations are O(1) per workflow interaction; projection writes should be indexed by `run_id`, `workflow_id`, and tenant/project identifiers.
- Fan-out risk is in projection and webhook replay, not in a single workflow execution.
- Hard limit for the spike should be a narrow cohort only; do not enable for all tenants initially.
- No caching is required for correctness paths. Observability queries can continue to use existing DB-backed views.

## 15. Failure modes

- Temporal worker down: detection via worker/task-queue health; recovery by restarting workers with no orchestration state loss.
- Temporal server unavailable: detection via start/signal failures; recovery via backend routing fallback for new runs, not silent partial execution.
- Duplicate answer event: detection via request ID or signal dedupe key; recovery by ignoring already-consumed semantic events.
- Activity side effect retried after partial success: detection via idempotency key and external correlation IDs; recovery by making activities idempotent or compensating explicitly.
- Projection lag or failure: detection via stale projection timestamp or sync status; recovery by replaying projection from workflow/query state.

## 16. Operational integrity

- Rollback strategy: route new runs back to legacy immediately via backend flag; do not migrate in-flight workflows back automatically during the spike.
- Non-reversible effects are external side effects emitted by activities, so each must carry idempotency keys and external correlation IDs.
- Dependency contracts:
  - Temporal server: bounded client timeout, retry on transient errors, fail fast on unavailable namespace/runtime.
  - Jira/GitHub/Discord activities: explicit timeouts, retry only on transient failures, idempotency keys for write operations, rate-limit-aware backoff.
  - Database projection writes: transactional write per projection update, retry on transient DB errors.
- Concurrency model:
  - Duplicate start requests must collapse on workflow ID policy.
  - Replayed answer/webhook events must be idempotent by request/event ID.
  - Out-of-order events must be ignored or rejected based on workflow state.
  - Concurrent updates must serialize through the workflow event loop rather than ad hoc DB checks.

## 17. Tests

- Invariant `resume once`: workflow test proving duplicate signals do not create duplicate resume side effects.
- Invariant `survives restart`: integration test where a workflow waits for input, worker restarts, then signal resumes and completes.
- Invariant `side-effect idempotency`: activity contract tests proving duplicate retries do not duplicate Discord/Jira/GitHub writes.
- Invariant `backend isolation`: routing test proving legacy paths are unchanged when the backend flag is `legacy`.
- Invariant `tenant boundary`: API/application test proving unauthorized start/signal/query attempts are rejected.
- Full-path proof: one production-path integration test for the selected spike workflow that starts, pauses for input, receives a real signal through the application boundary, resumes, and projects final state.

## 18. Verdict

✅ Proceed — design is appropriate and scoped
