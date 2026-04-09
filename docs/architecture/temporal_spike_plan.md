# Temporal Spike Plan

## Goal

Prove that Temporal can replace the brittle parts of the current custom orchestrator without forcing a full rewrite of `master-builder`.

## Recommendation Summary

- Treat Temporal as an orchestration substrate, not a domain rewrite.
- Start with the human-input boundary because that is where the current design is weakest.
- Keep Jira, GitHub, Discord, repo checkout, test execution, and finalization logic in existing services and wrap them as Activities.
- Use one Temporal Workflow per run. Do not introduce child workflows in the spike.
- Keep existing database tables as reporting and compatibility surfaces during the spike; do not make them the source of orchestration truth for the migrated path.

## Out Of Scope

- Full migration of every run path in the first pass
- Rewriting agent prompts or stage-specific business logic
- Replacing the admin UI
- Replacing every worker process with Temporal on day one

## Phase 1: Foundation And Human-Input Vertical Slice

### Objective

Get one end-to-end path running under Temporal for the most failure-prone boundary: waiting for human input, surviving process restarts, and resuming exactly once on reply.

### Scope

- Add local Temporal runtime support for development and spike testing.
- Add the Python Temporal SDK and a dedicated worker entrypoint.
- Introduce a single workflow, `IssueRunWorkflow`, owned by Temporal.
- Move this sequence into Temporal without rewriting the stage engine:
  1. enqueue the initial run
  2. process that exact queued run through an activity adapter into existing services
  3. if the run reaches `waiting_for_input`, keep the same Temporal workflow alive
  4. receive the human-input answer through a workflow Update
  5. enqueue the resume attempt through an activity and continue processing inside the same Temporal workflow
  6. finalize outcome
- Keep existing PM/Dev/Test/Review execution services intact behind activity adapters.

### Deliverables

- `docker compose` support for a local Temporal server or Temporal CLI dev server profile
- `temporalio` dependency and worker bootstrap
- New package layout:
  - `orchestrator/temporal/workflows/`
  - `orchestrator/temporal/activities/`
  - `orchestrator/temporal/worker.py`
- Adapter layer that translates Discord/webhook/admin resume actions into Temporal Updates on the owning `IssueRunWorkflow`
- Read-model sync so the existing admin/API surfaces can still show workflow status

### Success Criteria

- A run can enter `waiting_for_input`, survive worker restart, receive a reply, and resume once.
- Duplicate replies are ignored or safely deduplicated.
- The Temporal event history is enough to explain state transitions without relying on custom checkpoints.
- Existing non-migrated run paths continue to behave as before.

## Phase 2: One Full Temporal-Owned Run Path

### Objective

Move one representative run path fully under Temporal so orchestration, retries, pause/resume, and external events are owned by Temporal rather than the custom worker loop.

### Scope

- Introduce one workflow, `IssueRunWorkflow`, with steps for:
  1. run initialization
  2. PM stage
  3. Dev stage
  4. Test stage
  5. Review stage
  6. human-input wait and resume when needed
  7. finalization
- Wrap existing services as Activities rather than rewriting their internals.
- Route Discord replies, admin actions, and selected webhooks into workflow Signals or Updates.
- Add idempotency keys for external side effects such as comments, PR actions, and notifications.
- Keep the current orchestrator available behind a feature flag for non-migrated paths.

### Deliverables

- `IssueRunWorkflow` with typed input payloads and signal payloads
- Activity wrappers for:
  - queue/run initialization
  - stage execution adapters
  - human-input request delivery
  - finalization
  - status projection into existing tables
- Feature flag or routing rule such as `ORCHESTRATION_BACKEND=legacy|temporal`
- Metrics and logging for workflow start, signal receipt, activity retry, and completion outcome

### Success Criteria

- At least one chosen run class completes entirely through Temporal in local/dev environments.
- A worker restart during any pause or retry window does not lose orchestration state.
- Manual retries and replayed external events do not duplicate externally visible side effects.
- The parent/child worker supervision path is no longer on the critical path for the migrated run type.

## Phase 3: Controlled Cutover And Legacy Reduction

### Objective

Cut real traffic to Temporal in a controlled way, validate operational behavior, and shrink the legacy orchestration surface only after confidence is earned.

### Scope

- Enable Temporal-backed orchestration for a narrow cohort:
  - one tenant
  - one project
  - or one trigger class such as admin-started runs
- Keep rollback simple: new runs can be routed back to legacy immediately.
- Decommission legacy checkpoint/resume code only for fully migrated paths.
- Remove the parent/child supervisor dependency from migrated flows.

### Deliverables

- Routing controls for gradual enablement
- Operational runbooks for:
  - replayed events
  - stuck workflows
  - activity retry exhaustion
  - workflow termination and manual recovery
- Removal plan for legacy modules once Temporal owns the path
- Dashboard view that correlates run ID, Temporal workflow ID, and external issue/thread identifiers

### Success Criteria

- The pilot cohort runs stably for at least one release cycle.
- Rollback is proven by routing new runs back to legacy without data repair.
- Legacy resume/checkpoint code can be deleted for the migrated path with no loss of observability.

## Proposed File And Module Shape

- Keep business logic in existing modules under `orchestrator/core/**`.
- Add a thin Temporal orchestration layer under `orchestrator/temporal/**`.
- Add an orchestration backend router in the application layer rather than branching inside domain logic.
- Keep API/webhook routes thin: translate inbound events into `start_workflow`, `signal_workflow`, or `query_workflow` application calls.

## Suggested First Implementation Sequence

1. Add Temporal dev runtime and SDK.
2. Implement `IssueRunWorkflow` for one run class plus one Update path from Discord/admin resume.
3. Add activity wrappers around existing run processing and resume/finalization services.
4. Add read-model projection into current status tables and admin views.
5. Extend `IssueRunWorkflow` to cover the selected run class end to end.
6. Feature-flag routing between legacy and Temporal backends.
7. Pilot on a narrow cohort before deleting legacy path pieces.

## Key Constraints

- Do not split into child workflows in the spike.
- Do not dual-write orchestration state as authoritative in both DB and Temporal.
- Do not migrate every path before the human-input path is stable.
- Do not let API routes or webhook handlers embed workflow logic directly.

## Questions The Spike Must Answer

- Does Temporal eliminate duplicate resume attempts for human input?
- Is the event history easier to reason about than the current checkpoint/snapshot model?
- Can existing stage services be wrapped as Activities without invasive rewrites?
- Is operational overhead acceptable compared with the current custom worker supervision model?
