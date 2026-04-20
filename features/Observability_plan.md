 # Application Observability and Audit Telemetry Plan

  ## Summary

  Build a two-plane observability system:

  1. Operational telemetry plane

  - OpenTelemetry across API, workers, Temporal, workflow runtime, and integrations
  - self-hosted Collector + Grafana + Loki + Tempo + Prometheus
  - optimized for real-time debugging, filtering, traces, and metrics

  2. Durable audit plane

  - app-owned, append-only, exportable, retention-controlled event history
  - stores full raw operational content after mandatory secret/PII redaction
  - supports compliance, retention guarantees, replay investigation, and tenant-scoped export

  This is one architecture with configurable retention/export policy per tenant, not separate systems by tier.

  ## Key Changes

  ### 1. Shared event contract across the whole app

  Add one generic event envelope used by both telemetry and audit emission:

  - actor context: tenant, project, user/agent, service, runtime
  - execution context: workflow type, execution id, workflow id, run id, operation id, operation type, issue key
  - event metadata: timestamp, level, category, source component, event kind
  - payload: raw structured content after redaction
  - correlation: trace id, span id, request/correlation id, causation id

  This must be app-wide, not workflow-specific.

  ### 2. Operational telemetry plane

  Instrument these boundaries with OpenTelemetry:

  - FastAPI ingress and admin APIs
  - webhook ingress
  - worker loops
  - workflow runtime
  - operation executor
  - Temporal worker/activity boundaries
  - Jira/GitHub/Discord adapters

  Emit:

  - traces for request/execution/operation flow
  - logs for structured runtime events
  - metrics for latency, retries, failures, queue depth, event throughput

  Infra target:

  - OpenTelemetry Collector
  - Loki for logs
  - Tempo for traces
  - Prometheus for metrics
  - Grafana for exploration

  ### 3. Durable audit plane

  Add a generic append-only audit event store for compliance and export.

  Audit records include:

  - workflow lifecycle transitions
  - operation lifecycle transitions
  - retries, resumes, cancellations
  - human-input requests and answers
  - external side-effect attempts and outcomes
  - low-level operational logs and tool/runtime output after redaction
  - links to large artifacts where needed, but default is to persist the raw event payload in the audit record

  Required guarantees:

  - immutable append-only write path
  - tenant-scoped retention policy
  - export by tenant / execution / run / time range
  - replay investigation support via ordered event history
  - same event contract as telemetry, but persisted durably

  ### 4. UI and product surface

  Execution detail should gain a right-side slide-out:

  - clicking a step opens the drawer
  - default view shows recent telemetry for that operation/execution
  - operator can switch between:
      - Live telemetry
      - Audit history
  - include filtering by level, source, attempt, and time window
  - keep execution page itself focused on state; logs/history live in the drawer

  Also add platform-level observability views later for:

  - tenant-wide telemetry
  - workflow-wide failures
  - export/audit management

  ### 5. Query/API changes

  Add backend-only query APIs; browser must not access telemetry backends directly.

  Needed interfaces:

  - GET /api/admin/executions/{execution_id}/telemetry
  - GET /api/admin/executions/{execution_id}/operations/{operation_id}/telemetry
  - GET /api/admin/executions/{execution_id}/audit
  - GET /api/admin/executions/{execution_id}/operations/{operation_id}/audit
  - POST /api/admin/audit/export

  Each supports:

  - cursor/time-range pagination
  - level/category filters
  - source filters
  - tenant-scoped authorization
  - stable ordering

  ### 6. Configurable retention in one architecture

  Use one storage/query architecture with per-tenant policy:

  - telemetry retention window
  - audit retention window
  - export availability
  - legal/compliance hold flags if needed later

  Do not build different subsystems by tier. Only vary policy/config.

  ## Implementation Changes

  ### Phase 1: Foundation

  - Define the shared event envelope and redaction boundary
  - add app-wide emitters/helpers so code does not hand-roll telemetry/audit payloads
  - keep secrets/PII redaction mandatory before both telemetry and audit writes

  ### Phase 2: Audit store

  - add append-only audit event model and persistence path
  - persist all workflow/run/operation lifecycle events plus low-level runtime/tool logs
  - link audit entries to execution, operation, and run context

  ### Phase 3: OpenTelemetry integration

  - add OTel SDK + Collector wiring
  - instrument API, workers, runtime, Temporal, and integrations
  - propagate correlation and trace context through webhook -> runtime -> worker -> Temporal/activity boundaries

  ### Phase 4: Query services

  - add backend query services for telemetry and audit retrieval
  - normalize Loki/Tempo responses into product-safe API payloads
  - add audit export service with tenant-scoped filtering

  ### Phase 5: Execution drawer

  - add right-side drawer on execution detail
  - clicking a step opens operation-scoped telemetry/audit
  - support live refresh plus historical pagination

  - Audit persistence tests:
      - append-only writes
      - ordered playback
      - execution/operation correlation
      - tenant scoping
  - Telemetry integration tests:
      - API request emits correlated trace/log/metric context
      - workflow/runtime/operation execution emits execution and operation identifiers
      - Temporal-backed paths preserve correlation
  - UI tests:
      - clicking a step opens the right-side drawer
      - drawer shows telemetry entries for that step
      - drawer can switch to audit history
  - Export tests:
      - export by tenant / execution / time range
      - retention policy enforcement
  - Regression tests:
      - workflow state remains DB-owned and does not depend on telemetry availability
      - UI still works when telemetry backend is unavailable and falls back only to audit history, not summaries

## Assumptions

- “Full raw content” means full operational content after mandatory secret/PII redaction, because repo policy forbids storing secrets or customer PII in logs.
- Audit history is a separate durable plane from live telemetry, even though both use the same event contract.
- The first rollout should prioritize execution/run/workflow paths before broad platform-wide observability pages.
- Retention/export behavior is configurable per tenant, but the storage/query architecture is shared.

## Current implementation status

Implemented in this branch:

- append-only `audit_events` persistence for workflow/run/operation/runtime events
- execution-level and operation-level admin APIs for telemetry and audit reads
- audit export as NDJSON, now gated by tenant policy
- tenant-level observability policy for:
  - audit retention days
  - audit export enablement
  - legal hold
- audit pruning that respects tenant retention policy and legal hold
- execution detail right-side drawer for `Live telemetry` and `Audit history`
- OpenTelemetry bootstrap for API, workers, Temporal worker, and runtime spans
- deeper Temporal instrumentation at the safe seams:
  - client outbound workflow calls
  - worker activity execution
- local self-hosted observability stack via Docker Compose:
  - OpenTelemetry Collector
  - Loki
  - Tempo
  - Prometheus
  - Grafana

Deliberately deferred:

- direct Loki and Tempo query proxying from the app backend
- tenant retention/legal-hold management beyond the current settings UI and enforcement layer
- broader platform-wide observability pages beyond execution detail
- replay-sensitive workflow-body instrumentation inside Temporal workflow classes
