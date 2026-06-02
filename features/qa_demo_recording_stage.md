# QA Demo Recording Stage

**Technical Objective**
- Add a production-grade QA demo recording capability to the delivery workflow.
- When enabled per project, PM must derive what needs to be demoed from the ticket and acceptance criteria.
- After PR creation and before the PR is marked ready for review, QA must record several live walkthrough demos, upload them to S3-compatible storage, and attach stable links to the PR.

**Parent Feature Link**
- Pending Jira sync.
- Canonical interim execution brief: use this markdown file as the engineering-child source of truth until Jira sync is restored.
- Intended Jira project: `MAB`
- Intended issue type: `Task`
- Intended labels: `engineering-child`, `master-builder`, `orchestrator`

**Behavior Slice**
- Add a project-level switch for demo recording.
- Extend PM output with structured demo requirements derived from the ticket.
- Add a `qa` workflow stage after PR publication and before ready-for-review signaling.
- Record several QA-style walkthroughs against the acceptance criteria, including variant and failure-oriented exercises.
- Support built-in run-owned capture for browser, iOS, and Android projects.
- Upload demo artifacts through MinIO/S3-compatible storage and attach resulting links to the PR.
- Block ready-for-review signaling until QA demo evidence exists or the run is explicitly retried and succeeds.

**Resolved Product Decisions**
- Delivery target: scale-ready / production grade.
- Demo format: live demos showing the feature is built and working.
- Demo scope source: PM agent derives required demos from the ticket and acceptance criteria.
- Storage: S3-compatible storage with MinIO support.
- Workflow order: PR first, QA second, ready-for-review signal last.
- Failure behavior:
  - recording and upload are retryable
  - if QA cannot produce usable demo evidence, the run must not become ready for review
- QA proof standard:
  - record several walkthroughs
  - mimic what a QA engineer would do
  - cover variants, repeat actions, and attempts to break the feature
- Supported provider standard for this ticket:
  - browser capture is built in through the browser recorder against the release website URL
  - iOS capture is built in through the native macOS/Xcode simulator recorder
  - Android capture is built in through the native Android/ADB recorder
  - desktop capture is intentionally outside this ticket and will be handled later
- Release readiness standard:
  - QA must use the run release as its source of truth
  - required release service URLs must be active before recording
  - API/backend services advertised by the release must be running before QA records demos

**Execution Readiness**
- Good To Do status:
  - objective is clear
  - acceptance criteria are defined in this brief
  - impacted components are identified
  - automation test layers are identified
  - scale-ready intent is explicit
  - main dependency still open is Jira ticket materialization / session access restoration
- Execution blocker boundary:
  - backlog/spec work can continue in markdown
  - code implementation still requires the engineering child issue to exist or Jira execution access to be restored

**Implementation Plan**
- Policy and project configuration:
  - extend `PolicyConfig` and project policy overrides with a project-level demo recording switch
  - expose the override on the project settings surface
  - surface effective policy state in project detail UI
- PM contract:
  - extend PM output shape to include structured demo requirements
  - define a durable payload for demo scenarios, expected outcomes, and misuse/failure exercises
  - persist PM-derived demo requirements in execution checkpoints
- Workflow orchestration:
  - extend the stage model from `pm -> dev -> test -> review` to `pm -> dev -> test -> review -> qa`
  - keep PR creation/update ahead of QA
  - gate PR-ready signaling on QA completion
  - support retry semantics for QA failures without degrading to ready-for-review
- QA stage runtime:
  - add a `qa` stage prompt and response contract
  - allow QA to consume PM demo requirements, test outputs, PR metadata, and repo state
  - require QA to return uploaded artifact links plus evidence summary
- Artifact storage:
  - add an S3-compatible demo artifact service with MinIO support
  - persist artifact metadata, stable URLs, and upload state
  - fail hard if the configured storage contract cannot be satisfied
- PR integration:
  - update PR content after creation with a deterministic demo evidence section
  - keep the PR not-ready-for-review until QA evidence is attached
  - ensure review signaling and PR comments reflect QA state accurately
- Observability and UI:
  - add `qa` to execution snapshot, token diagnostics, stage traces, run detail UI, and checkpoint codec
  - render QA status and demo links on the run detail page
  - expose QA artifact state in workflow diagnostics and event history
- Tests:
  - unit tests for policy merge, PM payload parsing, QA payload parsing, and storage service behavior
  - integration tests for workflow ordering, PR update gating, retry handling, and artifact persistence
  - UI automation for project switch visibility and run detail QA artifact display

**Module Map**
- Workflow contracts and persistence:
  - `orchestrator/core/workflow/runner.py`
  - `orchestrator/core/workflow/orchestrated_run_runner.py`
  - `orchestrator/core/workflow/checkpoint_codec.py`
  - `orchestrator/core/workflow/execution_snapshot.py`
  - `orchestrator/core/worker/workflow_request_factory.py`
- Runtime agents and tool policy:
  - `orchestrator/core/runtime/agents.py`
  - `orchestrator/core/runtime/tools.py`
  - `orchestrator/core/prompt_domain_models.py`
  - `orchestrator/prompts/workflow/*.j2`
- Policy and admin API:
  - `orchestrator/api/schemas.py`
  - `orchestrator/core/projects/policy.py`
  - `orchestrator/api/admin/project_service.py`
  - `admin-ui/lib/api.ts`
  - `admin-ui/components/tenant-project-details-page.tsx`
- Review and PR-ready flow:
  - `orchestrator/core/review/pr_ready.py`
  - `orchestrator/core/review/reviewer.py`
  - `orchestrator/core/signal_templates.py`
- Run detail and observability UI:
  - `admin-ui/app/(dashboard)/runs/[runId]/page.tsx`
  - `orchestrator/api/admin/token_diagnostics_service.py`
- Demo artifact storage:
  - new service/module likely under `orchestrator/core/`
  - likely new storage model + migration for durable demo artifacts

**How to Test**
- Unit:
  - `pytest` target for policy normalization and effective project override handling
  - `pytest` target for PM/QA checkpoint encode/decode and stage payload validation
  - `pytest` target for S3-compatible artifact upload and retry behavior
- Integration:
  - `pytest` target for orchestrated workflow ordering:
    - PR is created before QA
    - QA runs before PR-ready signaling
    - failed QA prevents ready-for-review
  - `pytest` target for PR attachment/update behavior with stable demo links
  - `pytest` target for QA retry path and terminal blocked/failed outcomes
- UI automation / E2E:
  - admin UI test for project-level demo switch
  - run detail test for QA stage visibility and demo link rendering
- Evidence:
  - demo video of the new workflow itself once implemented
- Regression / failure path:
  - reproduce storage upload failure and verify the run remains not-ready-for-review
  - reproduce QA evidence absence and verify review signaling is suppressed
- Real boundary under test:
  - workflow orchestration boundary must be validated without mocking the owned stage sequencing logic
  - UI workflow tests should hit the real backend API for run state and artifact display

**Done Criteria**
- Project settings expose a demo recording switch and persist it through effective policy.
- PM stage emits structured demo requirements derived from the ticket.
- QA stage exists as a first-class workflow stage with durable checkpoint persistence.
- PR-ready signaling is impossible before QA evidence exists.
- Demo artifacts upload to S3-compatible storage with MinIO-compatible behavior locally.
- Built-in browser, iOS, and Android providers are available without per-project recorder commands.
- QA fails hard if the run release is unavailable or advertised release service URLs are not active.
- PRs show stable demo links produced by QA.
- Retry paths exist for recording/upload failures and keep the run out of ready-for-review until success.
- Run detail and observability surfaces show QA status and demo artifacts.
- Automated tests cover the owned behavior changes at the correct layers.

**Technical Dependencies / Risks**
- Jira execution remains blocked until the engineering child issue is created or Jira access is restored.
- The current Jira MCP server is configured in Codex but disabled in the active session.
- The repo-local Atlassian OAuth connection currently fails token refresh and cannot create Jira issues from this shell.
- Adding a `qa` stage is a contract change across persistence, runtime prompts, UI, and diagnostics; partial rollout would leave the workflow inconsistent.
- S3-compatible artifact URLs must be durable enough for PR review and must not depend on local-only paths.

**Synced From Parent Revision**
- Source: interactive product clarification in this thread.
- Status: ready to sync into Jira once Jira creation is available.

**Notes / Links**
- Decision gate answers are resolved for this slice.
- The repo currently uses markdown feature specs in `features/` as planning artifacts before or alongside Jira synchronization.
