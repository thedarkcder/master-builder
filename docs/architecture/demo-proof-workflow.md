# Demo Proof Workflow Architecture

## Goal

Master Builder must produce automated, review-visible proof for every QA-demo-enabled ticket.

When a run reaches the post-review proof gate, Master Builder must:

1. Acquire exactly one scoped preview lease for the run/PR/commit proof scope.
2. Create or reuse the real preview release attached to that lease.
3. Advance from provider and worker events, not business-level polling loops.
4. Verify the required browser/API services are live.
5. Record real browser, iOS, and Android walkthroughs against the delivered feature.
6. Upload the recordings to S3/MinIO-compatible durable storage.
7. Attach playable evidence links to the PR.
8. Keep the PR blocked from ready-for-review until proof is complete.
9. Cleanup, expire, or reconcile every preview resource created for proof.

Manual recordings, fake proof pages, unchecked PR links, and unowned preview containers do not satisfy this goal.

## Design Decision

Use composable Temporal workflows coordinated by event contracts.

The main model is:

```text
RunWorkflow
  -> DemoProofWorkflow
      -> PreviewLease
      -> ReleaseWorkflow
      -> RecordingWorkflow(browser)
      -> RecordingWorkflow(ios)
      -> RecordingWorkflow(android)
      -> EvidenceUpload
      -> PullRequestEvidenceUpdate
      -> PreviewCleanupWorkflow
```

`DemoProofWorkflow` may be started by `RunWorkflow`, but it must also be independently triggerable for re-proofing, release validation, or cleanup recovery.

The preview environment is modeled as a leased proof resource. A release is the provider implementation used to satisfy that lease.

## Why The Previous Shape Fails

The previous shape is release-centric:

```text
run reviewed -> create release -> worker waits/requeues -> eventually try QA
```

That fails because:

- Preview lifecycle is split across run outcome policy, release rows, provider reconciliation, queue reclaims, and manual cleanup.
- Retries can create new provider artifacts without proving the previous artifacts were destroyed.
- The system cannot clearly answer whether a preview is valid, stale, superseded, abandoned, or safe to reuse.
- Business waiting leaks into worker polling and database requeue behavior.
- Disk, Docker, and Coolify artifacts accumulate because cleanup is not a first-class state.

The corrected shape is lease-centric:

```text
need proof -> acquire preview lease -> satisfy lease with release -> record evidence -> attach proof -> release/cleanup lease
```

## Core Concepts

### Proof Scope

The identity of the proof request.

Fields:

- `tenant_id`
- `project_id`
- `proof_scope_id`
- `run_id` optional
- `issue_key` optional
- `pr_number` optional
- `commit_sha`
- `source_branch`
- `trigger_mode`

Supported trigger modes:

- `from_run`: normal run after review.
- `from_pr`: prove an existing PR and commit.
- `from_release`: record against an existing live release.
- `retry_recording`: reuse a valid lease and rerun missing recordings.
- `cleanup_only`: cleanup expired, superseded, or orphaned proof resources.

### Preview Lease

The business-owned right to use one preview environment for one proof scope.

Lease identity:

```text
tenant_id + project_id + proof_scope_id + pr_number + commit_sha
```

Hard invariant:

```text
At most one active preview lease exists per proof scope.
```

Provider IDs, container names, route names, and deployment UUIDs are implementation details of the lease. They must not be treated as product truth.

### Evidence

Evidence is durable proof that a target was exercised against a specific release context.

Required metadata per recording:

- `artifact_url`
- `object_key`
- `capture_target`: `browser`, `ios`, or `android`
- `capture_reference`
- `recording_name`
- `content_sha256`
- `release_commit_sha`
- `release_context_sha256`
- `created_at`

Screenshots alone are insufficient for workflow proof. PR links alone are insufficient unless they point to uploaded artifacts with valid metadata.

## Workflow Responsibilities

### RunWorkflow

Owns ticket delivery lifecycle.

Responsibilities:

- Run PM, implementation, tests, and review.
- If QA demo recording is enabled, start or signal `DemoProofWorkflow`.
- Wait for `DemoProofCompleted` or `DemoProofBlocked`.
- Mark ready-for-review only after demo proof succeeds.
- Surface proof state in run status.

Non-responsibilities:

- It does not manage provider cleanup directly.
- It does not poll release status.
- It does not record videos itself.

### DemoProofWorkflow

Owns proof lifecycle.

Responsibilities:

- Resolve proof scope.
- Acquire/reuse/supersede preview lease.
- Start or signal `ReleaseWorkflow`.
- Wait for release events.
- Verify required services.
- Start platform recording workflows.
- Wait for required target evidence.
- Upload/validate artifacts.
- Attach evidence to PR.
- Start or schedule cleanup.
- Return complete or blocked result.

### ReleaseWorkflow

Owns release creation and liveness for a lease.

Responsibilities:

- Create provider release for the lease.
- Emit release lifecycle events.
- Keep provider identifiers scoped to the lease.
- Fail deterministically if provider creation fails.

Non-responsibilities:

- It does not decide whether PR can become ready-for-review.
- It does not create extra releases for retries unless the lease explicitly supersedes the old one.

### RecordingWorkflow

Owns one platform recording target for one release context.

Targets:

- `browser`
- `ios`
- `android`

Responsibilities:

- Generate or receive executable QA walkthrough scenarios.
- Execute real recorder against the release.
- Validate local video output.
- Emit `RecordingCompleted` or `RecordingFailed`.

### PreviewCleanupWorkflow

Owns teardown and orphan recovery.

Responsibilities:

- Destroy superseded, expired, failed, abandoned, or completed preview leases according to policy.
- Reconcile provider resources back to lease state.
- Mark `cleanup_failed` if provider cleanup cannot be proven.
- Retry cleanup without creating new proof resources.

## State Machines

### DemoProofWorkflow State

```text
requested
lease_acquiring
lease_acquired
release_requested
release_provisioning
release_live
services_verifying
services_verified
recording
evidence_uploading
evidence_uploaded
pr_attaching
pr_attached
cleanup_scheduled
complete
blocked
```

Terminal states:

- `complete`
- `blocked`

### PreviewLease State

```text
requested
active
provisioning
live
recording
evidence_complete
released
destroying
destroyed
failed
superseded
expired
cleanup_failed
```

Legal transitions:

- `requested -> active`
- `active -> provisioning`
- `provisioning -> live`
- `live -> recording`
- `recording -> evidence_complete`
- `evidence_complete -> released`
- `released -> destroying`
- `destroying -> destroyed`
- `active|provisioning|live|recording -> failed`
- `active|provisioning|live|failed -> superseded`
- `released|failed|superseded|expired -> destroying`
- `destroying -> cleanup_failed`
- `cleanup_failed -> destroying`

### Evidence State

```text
planned
recording
recorded_local
uploading
uploaded
linked_to_pr
invalid
failed
```

## Event Model

Business state advances from events/signals.

Required events:

- `DemoProofRequested`
- `ProofLeaseAcquired`
- `ProofLeaseSuperseded`
- `ReleaseRequested`
- `ReleaseProvisioning`
- `ReleaseLive`
- `ReleaseFailed`
- `RouteReady`
- `ServiceVerificationPassed`
- `ServiceVerificationFailed`
- `RecordingStarted`
- `RecordingCompleted`
- `RecordingFailed`
- `EvidenceUploaded`
- `EvidenceUploadFailed`
- `PREvidenceAttached`
- `PREvidenceAttachFailed`
- `PreviewCleanupRequested`
- `PreviewCleanupCompleted`
- `PreviewCleanupFailed`
- `DemoProofCompleted`
- `DemoProofBlocked`

Provider polling is allowed only inside adapters as bounded monitoring. The business workflow must not rely on worker queue polling to decide what state it is in.

## Event Payload Contract

Every event must include:

- `event_id`
- `event_type`
- `occurred_at`
- `tenant_id`
- `project_id`
- `proof_scope_id`
- `lease_id` when applicable
- `release_id` when applicable
- `commit_sha` when proof or release related
- `producer`
- `idempotency_key`

Recording events must also include:

- `capture_target`
- `capture_reference`
- `recording_name`
- `local_artifact_path` for internal handoff only, or uploaded artifact metadata if already uploaded
- `content_sha256`
- `release_context_sha256`

## Idempotency And Ordering

Duplicate events:

- Must be accepted idempotently.
- Must not create duplicate leases, releases, recordings, uploads, or PR evidence sections.

Out-of-order events:

- `RecordingCompleted` before `ReleaseLive` is invalid unless tied to an already validated release context.
- `EvidenceUploaded` before `RecordingCompleted` is invalid.
- `PREvidenceAttached` before required evidence count is complete is invalid.
- `CleanupCompleted` for unknown lease is orphan evidence and must be reconciled, not ignored silently.

Missing events:

- Workflow remains in waiting state until bounded timeout.
- On timeout, state becomes blocked or cleanup is requested depending on step.

## Lease Acquisition Rules

When acquiring a lease:

1. Find active leases for the same proof scope.
2. If one valid lease exists for the same commit and provider context, reuse it.
3. If one invalid, failed, stale, or mismatched lease exists, supersede it and request cleanup before replacement.
4. If more than one active lease exists, block and reconcile before creating another.
5. If disk/provider capacity preflight fails, block before creating a release.

Active lease states:

- `active`
- `provisioning`
- `live`
- `recording`
- `evidence_complete`
- `cleanup_failed`

Inactive lease states:

- `destroyed`
- `failed` after cleanup requested or completed
- `superseded`
- `expired`

## Cleanup Policy

Cleanup is not optional.

Cleanup triggers:

- Successful proof after evidence is uploaded and attached to PR.
- Release creation failure.
- Recording failure after retry budget is exhausted.
- Superseded lease.
- PR closed or merged.
- TTL expiry.
- Orphan provider resource detected.

Retention policy:

- Keep successful preview only if configured; otherwise destroy after PR evidence is attached.
- Keep latest failed preview for a short debug TTL only.
- Never keep superseded previews indefinitely.
- Uploaded videos outlive preview cleanup.

Cleanup must prove provider-side state, not only mark local state.

## Capacity Preflight

Before starting expensive release work, the workflow must verify capacity.

Minimum checks:

- Docker disk availability.
- Docker build cache pressure.
- Active build count.
- Postgres health.
- Coolify health.
- MinIO/S3 reachability.
- Required recorder runtime availability for requested targets.

If capacity is below threshold, block before creating a release.

## PR Evidence Contract

PR evidence section must include:

- Required capture targets.
- Required recording counts.
- One link per recording.
- Target metadata.
- Release commit SHA.
- Release context SHA.
- Artifact content SHA.

The PR must remain draft or blocked until evidence is attached and verified.

## Completion Criteria

The goal is complete only when current evidence proves:

- A run with QA demo recording enabled starts proof after review.
- `DemoProofWorkflow` is created and visible as its own workflow.
- The workflow can also be triggered independently by proof scope.
- One active preview lease is enforced for the proof scope.
- Release creation is called through workflow orchestration.
- Release state advances through events/signals.
- Browser recording is produced against the real release.
- iOS recording is produced against the real release.
- Android recording is produced against the real release.
- Recordings are uploaded to S3/MinIO-compatible storage.
- Artifact URLs are reachable.
- PR evidence links are attached.
- PR is not ready-for-review before proof is attached.
- Superseded/failed/successful preview resources are cleaned up or TTL-managed.
- Orphan Coolify resources are detected and reconciled.
- Low disk/capacity blocks before release creation.

## Verification Matrix

| Requirement | Evidence Required |
| --- | --- |
| Run starts proof after review | Run workflow history shows `DemoProofWorkflow` started after review result |
| Independent trigger works | Manual proof trigger creates or signals `DemoProofWorkflow` by proof scope |
| One active lease | DB/query or workflow state shows one active lease per scope under concurrent/retry attempts |
| Event-driven release wait | Workflow history shows release events/signals, not worker polling as business control |
| Browser proof | Uploaded browser video link with SHA metadata and PR link |
| iOS proof | Uploaded iOS video link with SHA metadata and PR link |
| Android proof | Uploaded Android video link with SHA metadata and PR link |
| Real release target | Recording metadata commit/context matches preview release commit/context |
| PR gate | PR remains draft/blocked before evidence and ready only after evidence |
| Cleanup | Provider resources for superseded/completed lease are destroyed or TTL-marked |
| Orphan reconciliation | Orphan provider resource is detected and cleanup workflow is started |
| Capacity preflight | Low disk/provider capacity blocks before release creation |

## Test Plan

Unit tests:

- Preview lease state transitions.
- One-active-lease invariant.
- Event idempotency.
- Out-of-order event rejection.
- Required recording count validation.

Integration tests:

- `RunWorkflow` starts/signals `DemoProofWorkflow`.
- `DemoProofWorkflow` starts/signals `ReleaseWorkflow`.
- Release event advances proof workflow.
- Recording events complete required target set.
- PR update waits for required evidence.
- Cleanup workflow runs after success/failure/supersession.

Failure tests:

- Release failed blocks proof and cleanup is requested.
- Recorder failure retries without creating another preview lease.
- Upload failure keeps local evidence invalid until upload succeeds.
- PR update failure does not mark proof complete.
- Cleanup failure becomes `cleanup_failed` and reaper retries.
- Low disk blocks before provider release creation.

End-to-end proof:

- Run against a real project PR.
- MB creates/reuses one preview lease.
- MB creates a real release.
- MB records browser/iOS/Android walkthroughs.
- MB uploads videos.
- MB attaches PR links.
- MB blocks ready-for-review until links exist.
- MB destroys or TTL-schedules the preview lease.

## Non-Goals

- Desktop recording in this phase.
- Manual evidence upload as proof.
- Blind Docker prune as cleanup.
- Replacing provider adapters with product state.
- Treating screenshots as sufficient proof for workflow acceptance.

## Main Thread Goal Update

Replace the current implicit QA demo/release polling path with a composable, event-driven Temporal proof architecture:

```text
RunWorkflow -> DemoProofWorkflow -> PreviewLease -> ReleaseWorkflow -> RecordingWorkflows -> EvidenceUpload -> PR Evidence -> CleanupWorkflow
```

The objective is complete only when MB itself can produce browser/iOS/Android demo proof against a real leased preview release, attach durable evidence to the PR, and cleanup or TTL every preview resource it creates.
