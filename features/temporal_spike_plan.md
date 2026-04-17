
  # Temporal Migration Plan: Central Run Engine First, Engine-Agnostic by Design

  ## Summary

  Build a new branch from the current branch, not codex/temporal-spike-phase-plan.
  The goal is to adopt Temporal as the first execution substrate while keeping Run as
  the central domain model and introducing a first-class operation/effect lifecycle
  that remains valid even if Temporal is replaced later.

  This plan does not lead with team_run or team-defined workflow graphs. It first
  stabilizes the platform around:

  - one canonical RunExecutionWorkflow
  - one shared Operation / Attempt / Projection model
  - one orchestration adapter boundary so Temporal is infrastructure, not the
    business model

  Recommended branch intent:

  - new branch from the current branch
  - branch contains the full migration implementation plan and initial architecture
    contracts
  - no reuse of the team_run cutover as the source of truth

  ## Architecture Decisions

  ### 1. Central run domain stays

  - Run remains the first-class execution concept.
  - Existing policy/admission/finalization semantics stay centralized around the run
    domain.
  - Temporal replaces the brittle orchestration substrate, not the meaning of a run.

  ### 2. Orchestration must be engine-agnostic

  Introduce one narrow orchestration port owned by the app, with Temporal as the
  first implementation.

  Core port:

  - start_run_workflow(run_id, workflow_id, tenant_id, project_id, issue_key)
  - signal_human_input(workflow_id, request_id, actor_ref)
  - signal_external_event(workflow_id, event_type, correlation_key, payload)
  - query_workflow_state(workflow_id)
  - cancel_workflow(workflow_id, reason)
  - retry_workflow_operation(workflow_id, operation_id)

  Rules:

  - no domain code depends directly on Temporal workflow handles, search attributes,
    activity payload classes, or event history
  - workflow IDs are app-owned business IDs, not Temporal-owned semantics
  - UI, notifications, and admin views read app projections, not Temporal directly

  ### 3. Introduce a first-class operation/effect model

  Every required side effect after a state transition becomes a durable operation.

  New domain concepts:

  - Workflow
      - logical long-running process, initially run_execution
  - WorkflowState
      - durable current state for the workflow
  - Operation
      - one required side effect, for example jira_parent_update, jira_child_fanout,
        discord_projection, automation_dispatch
  - OperationAttempt
      - one try against an external or internal system
  - Projection
      - user/admin-visible outputs such as notifications, Jira comments, Discord
        posts, pills, and status banners

  Required invariants:

  - a workflow is not complete until all required operations are either:
      - completed, or
      - in a durable failed state or waiting-for-input state with explicit retry/failure metadata
  - retry semantics are based on operation policy, not webhook delivery
  - external writes are idempotent by operation key and effect fingerprint

  ### 4. Temporal is the first engine, not the only engine

  Implement:

  - TemporalOrchestrationEngine
  - leave room for:
      - DatabaseOrchestrationEngine
      - Noop/TestEngine

  Do not use Temporal as:

  - the canonical business schema
  - the only audit trail
  - the only query/read model
  - the only place where failure and retry metadata are visible

  ## Implementation Plan

  ### Phase 0: Branch and architecture baseline

  - Create a new branch from the current branch for the migration program.
  - Add architecture docs that define:
      - run-centric orchestration model
      - operation/effect lifecycle
      - orchestration engine port
      - migration phases and guardrails
  - Explicitly mark codex/temporal-spike-phase-plan as reference material only, not
    the migration base.

  Acceptance:

  - one architecture document defines the target model and migration rules
  - one staff-review document records the invariants and rollback constraints
  - one runbook explains local Temporal development and failure handling

  ### Phase 1: Orchestration engine port and read-model contracts

  Add new app-owned contracts before any Temporal cutover.

  Core additions:

  - orchestration engine interface module
  - workflow state query model used by admin/UI
  - operation lifecycle tables:
      - workflow_operations
      - workflow_operation_attempts
      - optional workflow_effect_projections if projection tracking needs a separate
        table
  - projection model updates so admin notifications, run detail, and issue states can
    reflect failed operations and waiting-for-input states

  Behavior:

  - existing legacy run path continues to operate
  - new tables may be written opportunistically for legacy flows where possible
  - no Temporal execution yet

  Acceptance:

  - app can persist and query operations independently of the execution engine
  - a Jira-style side-effect failure can be represented as:
      - workflow state
      - operation status
      - blocker
      - notification
  - existing admin screens can show failed state from app tables without Temporal

  ### Phase 2: Temporal-backed RunExecutionWorkflow

  Implement a single Temporal workflow for runs, with no team-run abstraction.

  Workflow shape:

  - RunExecutionWorkflow
      - initialize run
      - execute PM stage
      - execute Dev stage
      - execute Test stage
      - execute Review stage
      - wait for human input when needed
      - finalize run outcome
      - schedule and monitor required operations

  Activities:

  - wrap existing stage services as activities instead of rewriting business logic
  - wrap side effects as operation executors
  - every activity that writes externally must accept:
      - operation ID
      - idempotency key
      - correlation metadata

  Required routing:

  - add orchestration_backend=legacy|temporal
  - route selected runs to Temporal while keeping legacy available
  - keep read-model projection into existing Run, WorkflowExecution, admin status,
    and notification surfaces

  Acceptance:

  - one run can start under Temporal and complete through current PM/Dev/Test/Review
    services
  - one run can enter waiting_for_input, survive worker restart, receive one answer,
    and resume exactly once
  - duplicate reply or replayed webhook does not duplicate run resume or external
    writes
  - run detail and admin status remain accurate through app projections

  ### Phase 3: Operation/effect execution under Temporal

  Move post-state side effects into first-class operations executed and tracked by
  the workflow.

  Initial operation types to migrate:

  - jira_parent_update
  - jira_child_fanout
  - jira_comment_projection
  - discord_followup_projection
  - notification_emit
  - automation_dispatch

  Policy model:

  - transient failures: retry with bounded backoff
  - permanent content/config failures: mark failed, emit notification, stop retrying
  - human-in-the-loop cases: transition to waiting_for_input until a reply arrives

  Example target behavior for MAB-215 class failures:

  - PM brief completes
  - jira_child_fanout operation runs
  - Jira returns CONTENT_LIMIT_EXCEEDED
  - workflow becomes failed
  - operation is failed
  - failure category is content_limit
  - notification opens with concrete remediation
  - no vague comment-only state

  Acceptance:

  - every required side effect after run/PM progression is represented as an
    operation
  - one failed Jira operation produces a durable failure record and visible notification
  - replaying the same inbound event does not create duplicate outward writes
  - operator can see current operation state and last attempt reason in admin
    surfaces

  ### Phase 4: Legacy cutover for central run engine

  Once the Temporal run path is stable, cut over the central run engine.

  Steps:

  - select a narrow cohort first:
      - one tenant, project, or trigger class
  - make Temporal authoritative for run orchestration in that cohort
  - keep legacy fallback for new runs only
  - stop extending the legacy run orchestration path
  - delete legacy resume/checkpoint logic only after parity is proven for migrated
    run flows

  Acceptance:

  - pilot cohort runs stably for one release cycle
  - rollback to legacy is proven for new runs
  - legacy human-input resume logic is removable for the migrated cohort
  - no team_run abstraction is required for central run execution

  ### Phase 5: Non-run workflows, then optional team workflows

  Only after the central run engine and operation model are stable:

  - add non-run workflows such as:
      - PmParentWorkflow
      - ReleaseWorkflow
      - SecurityReviewWorkflow
      - DocumentationWorkflow
  - all of them reuse:
      - the same orchestration port
      - the same operation model
      - the same notification/projection layer

  Only after those are proven should the platform revisit:

  - team-defined workflow graphs
  - custom workflow composition
  - generalized workflow catalogs

  Acceptance:

  - at least one non-run workflow uses the same operation lifecycle successfully
  - the run engine remains central and intact
  - team-defined workflows remain explicitly out of scope until the above is stable

  ## Important Interfaces and Contracts

  ### Orchestration engine

  Public app-owned interface:

  - start_run_workflow(...)
  - signal_human_input(...)
  - signal_external_event(...)
  - query_workflow_state(...)
  - cancel_workflow(...)
  - retry_workflow_operation(...)

  Implementation notes:

  - Temporal implementation maps these to workflow start/update/query
  - fallback implementation may later use DB-backed orchestration
  - callers must not depend on engine-specific types

  ### Workflow and operation state

  Minimum statuses:

  - workflow:
      - pending
      - running
      - waiting_for_input
      - completed
      - failed
      - cancelled
  - operation:
      - pending
      - running
      - retrying
      - failed
      - completed

  ### Failure categories

  Initial categories:

  - reauth_required
  - content_limit
  - rate_limited
  - transient_external_failure
  - contract_invalid
  - missing_input
  - authorization_failed

  ### Projection rules

  - projections are derived from workflow and operation state
  - projections are never the source of truth
  - notifications, Jira comments, Discord messages, and pills must be idempotent by
    unresolved state fingerprint

  ## Test Plan and Acceptance Criteria

  ### Contract tests

  - orchestration engine contract tests:
      - start
      - signal
      - query
      - cancel
      - retry workflow operation
  - operation lifecycle tests:
      - pending to completed
      - retryable failure to retrying
      - permanent failure to failed
  ### Temporal integration tests
  - run starts once for a given workflow ID
  - worker restart during waiting_for_input preserves state
  - duplicate input signal resumes exactly once
  - activity retries do not duplicate Jira/Discord side effects
  - failed operation is visible in app read models

  ### End-to-end scenarios

  - successful run under Temporal with no human input
  - Run remains the central execution domain throughout the migration.
  - team_run and team-defined workflow composition are explicitly deferred.
  - Existing stage services remain intact and are wrapped as activities rather than
    rewritten.
  - The operation/effect model is app-owned and remains valid even if Temporal is
    replaced later.



› no we are replacing run. so we are spliting th current spike work and moving the
  team run in to another pr.


• That changes the migration shape materially. I’m reframing it as: replace the current
  run engine with a Temporal-backed run engine now, and split all team_run
  generalization into a separate PR. I need to lock three decisions so the plan is
  decision-complete.

• Questions 3/3 answered
  • In the first PR, how much of the existing Run API/UI contract should stay stable
    while the engine underneath is replaced?
    answer: None of the above
    note: a run is an execution of a workflow. but we are replacing the internals. the
          current run oae is fine as it is for an development team workflow run. dont
          leave compatibility surfaces laying around
  • What should happen to the current team-run work in relation to the first PR?
    answer: Defer team-run entirely (Recommended)
  • Even though the first implementation will use Temporal, how should backend
    selection work in the first PR?
    answer: None of the above
    note: temporal is a implementation. the legacy will move to an implementation. then
          we can also have a db back implenation if we like
