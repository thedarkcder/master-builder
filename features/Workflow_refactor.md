 # Workflow Runtime Refactor Plan

  ## Summary

  Stop designing around parent_planning. It is only one workflow instance of a broader system. The redesign should treat workflow types as database-defined state machines with database-defined
  operations, and the runtime should execute any workflow type through one generic boundary.

  The target architecture is:

  - workflow types define states, transitions, operations, retry policy, and optional projections in the database
  - executions are instances of those types
  - one app-owned WorkflowRuntime owns lifecycle semantics for all workflows
  - Temporal is the first WorkflowEngine implementation behind that runtime, not the workflow model itself
  - run pages remain a projection of execution state, not a separate orchestration system

  This replaces the current shallow ownership spread across run services, parent-planning projection code, retry code, admin read models, and direct engine usage.

  ## Key Changes

  ### 1. Make workflow definitions fully generic and DB-owned

  - Treat a workflow type as a persisted definition, not a code concept.
  - Define, in the database:
      - workflow type key and display metadata
      - orchestration backend
      - state list
      - transition list
      - operation list
      - retry policy
      - wait-for-input capability
      - optional projection capabilities
  - Remove any remaining code paths that infer behavior from workflow type names or operation-name heuristics.
  - Replace helper logic like “supports child issue links” with definition metadata or operation metadata stored in the catalog.

  ### 2. Introduce one generic runtime boundary

  Add one app-owned interface:

  class WorkflowRuntime:
      def start(...)
      def advance(...)
      def resume_input(...)
      def retry_operation(...)
      def query(...)

  Internal ports behind it:

  - WorkflowStore
  - WorkflowEngine
  - WorkflowIntegrationRouter
  - WorkflowProjectionPublisher

  Rules:

  - all lifecycle state changes go through WorkflowRuntime
  - callers do not build engines directly
  - callers do not compute next-step, failure, or waiting state
  - callers do not know about Jira auth, Discord clients, or Temporal handles

  ### 3. Treat executions as materialized definitions

  - On execution creation, materialize the full operation set from the workflow type.
  - Overlay live state onto those operations:
      - pending
      - running
      - waiting_for_input
      - retrying
      - completed
      - failed
  - Eliminate any “observed operations only” behavior.
  - Keep only these execution interruption states:
      - failed
      - waiting_for_input

  ### 4. Unify run lifecycle under execution lifecycle

  - Keep Run as a product-facing projection only.
  - Make workflow execution the orchestration truth.
  - Refactor run start/resume/query/retry to call WorkflowRuntime.
  - Derive run state from execution state and operation state.
  - Remove direct orchestration ownership from run services once runtime coverage is complete.

  ### 5. Move workflow-specific logic into workflow handlers, not framework seams

  - parent_planning becomes one workflow handler plugged into the generic runtime.
  - Any other current state machine workflow should fit the same contract.
  - Workflow-specific code may decide:
      - how to interpret input events
      - which transition to take
      - which operations to schedule
      - whether the execution is now waiting_for_input or failed
  - Workflow-specific code may not:
      - bypass runtime state updates
      - own persistence directly
      - talk to adapters directly
      - invent execution status outside runtime rules

  ### 6. Put integrations behind adapter routing

  - Remove generic workflow dependencies on Jira-specific context functions like tenant_jira_oauth_context_fn.
  - Introduce an integration-routing boundary keyed by operation target system or integration capability.
  - Keep Jira credential resolution and Jira client behavior entirely inside the Jira adapter.
  - Apply the same pattern for Discord, notifications, and future systems.

  ### 7. Keep Temporal as a replaceable engine

  - WorkflowEngine is an app-owned port.
  - Implement:
      - TemporalWorkflowEngine
      - LegacyWorkflowEngine during migration
  - Do not let:
      - Temporal workflow IDs
      - search attributes
      - activity payloads
      - Temporal history
        become canonical business contracts.
  - The app database remains the source of truth for workflow definitions, executions, operations, attempts, and failure state.

  ## Implementation Plan

  ### Phase 1: Definition contract cleanup

  - Audit the current workflow catalog schema and extend it to fully describe workflow behavior generically.
  - Remove remaining code-level workflow-name heuristics.
  - Add definition metadata needed for UI and link/projection behavior so the UI does not infer from names.
  - Validate required workflow-type fields at the API contract and DB level only.

  ### Phase 2: Runtime boundary completion

  - Expand the current WorkflowRuntime first cut to include advance(...).
  - Move start/resume/query/retry callers onto the runtime exclusively.
  - Introduce internal store/engine/integration/projection ports.
  - Make runtime return a stable execution result shape used by admin, workers, and human-input flows.

  ### Phase 3: Workflow handler model

  - Add a generic workflow-handler contract used by runtime for per-type logic.
  - Implement the first handlers for current workflow families:
      - run execution
      - parent planning
      - any other active workflow type already represented in the state machine
  - Route event interpretation and transition planning through the handler, but leave persistence and adapter execution in runtime.

  ### Phase 4: Operation execution and retry consolidation

  - Move operation scheduling, attempt recording, retryability, and failure recording behind runtime-owned services.
  - Support true per-operation retry through runtime, not execution-level retry only.
  - Ensure failed operations and waiting-for-input states are persisted consistently for all workflow types.

  ### Phase 5: Read-model and UI consolidation

  - workflows page shows workflow types only.
  - executions page shows workflow instances only.
  - Workflow type detail shows:
      - states
      - transitions
      - operations
      - retry policy
      - projection definitions
  - Execution detail shows:
      - current state
      - completed/failed/pending/retrying operations
      - latest failure reason
      - waiting-on-human-input status
      - next step
      - links/projections generated by definition metadata
  - Retry actions operate on failed operations, not just whole executions.

  ### Phase 6: Legacy seam removal

  - Remove old direct engine-factory usage outside runtime.
  - Remove duplicated lifecycle logic from:
      - run services
      - workflow-specific projection helpers
      - admin retry/read seams
  - Keep only adapters and workflow handlers outside runtime core.

  ## Public Interfaces / Types

  - WorkflowRuntime becomes the only orchestration entry boundary for app code.
  - Workflow type API must expose enough metadata for:
      - states
      - transitions
      - operations
      - retry policy
      - projection capabilities
  - Execution API must expose:
      - workflow type reference
      - execution status
      - operation list with status and retryability
      - latest failure reason
  - No public/admin contract should expose Temporal-specific identifiers or semantics as the business model.
  ## Test Plan
  - Definition-driven tests:
      - workflow type with a given operation set materializes all operations on execution creation
      - transition metadata drives next-step computation without code heuristics
      - duplicate events do not duplicate side effects
  - Adapter tests:
      - Jira adapter resolves credentials internally
      - runtime never depends on Jira-specific auth callbacks
  - Read-model tests:
      - workflow type pages show definition data only
      - execution pages show live materialized state only
  - Regression tests:
      - current parent planning flow works as one workflow handler, not as a special framework seam

  ## Assumptions and Defaults

  - parent_planning is treated only as one workflow handler and migration target, not as the design anchor.
  - Run UX remains in place, but the underlying orchestration source of truth is the execution model.