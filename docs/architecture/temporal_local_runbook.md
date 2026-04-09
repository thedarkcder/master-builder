# Local Temporal Runbook

This runbook is for the Temporal spike in the `temporal-spike` worktree. It uses Docker Compose to run a local Temporal stack and the containerized `temporal-worker`, then exercises the single-owner `IssueRunWorkflow` path end to end.

## What This Proves

- The app can start one Temporal workflow for a run.
- The same Temporal workflow can process the queued run, pause on `waiting_for_input`, accept a human-input update, and continue with a resumed attempt.
- The orchestration backend can be switched through environment/config instead of code edits.

## Services Added

- `temporal-postgres`
- `temporal`
- `temporal-ui`
- `temporal-worker`

The Temporal services are behind the Compose profile `temporal`.

## Required Environment

Set the normal orchestrator secrets you already use for local Compose, plus these Temporal-specific overrides:

```bash
export ORCHESTRATOR_ORCHESTRATION_BACKEND=temporal
export ORCHESTRATOR_TEMPORAL_TARGET_HOST=temporal:7233
export ORCHESTRATOR_TEMPORAL_NAMESPACE=default
export ORCHESTRATOR_TEMPORAL_TASK_QUEUE=master-builder
```

## Start The Stack

Bring up the normal app services plus the Temporal profile:

```bash
docker compose --profile temporal up --build \
  postgres redis mailpit api run-worker webhook-worker temporal-postgres temporal temporal-ui temporal-worker
```

Useful endpoints:

- API: `http://localhost:4000`
- Admin UI: `http://localhost:4100`
- Temporal gRPC: `localhost:7233`
- Temporal UI: `http://localhost:8233`

## Start A Temporal-Owned Run

Use the normal CLI run command with Temporal mode enabled.

```bash
docker compose exec api python -m orchestrator run \
  --tenant <tenant_id> \
  --issue <issue_key>
```

The command prints JSON including:

- `run_id`
- `issue_key`
- whether the run was enqueued

At this point:

- the queued run row exists in the orchestrator database
- the Temporal workflow has been started under the workflow ID `issue-run:<workflow_execution_id>`
- the Temporal workflow will claim and process that exact queued run without going through the legacy run poller

You can inspect it in the Temporal UI after the run is created.

## Answer A Human-Input Request And Resume

If the run reaches `waiting_for_input`, answer it through the existing application boundary, for example a Discord reply or the existing reply handling path. The API/service layer will route that answer into the owning `IssueRunWorkflow` as a Temporal Update.

Expected result:

- the request moves from `pending` -> `answered` -> `consumed`
- the existing Temporal workflow receives the Update
- the resume activity enqueues the next run attempt
- the same Temporal workflow continues by processing the resumed run ID

## Observe The Result

Watch the worker logs:

```bash
docker compose logs -f temporal-worker run-worker api
```

What you should see:

- the Temporal workflow starts once for the run
- the Temporal worker claims and processes the queued run
- if human input is needed, the workflow waits for an Update and then resumes the next attempt without starting a second Temporal workflow

## Rollback

To switch back to the legacy orchestrator for new work:

```bash
export ORCHESTRATOR_ORCHESTRATION_BACKEND=legacy
docker compose up -d api run-worker webhook-worker
```

You do not need to delete the Temporal services to revert new requests back to the legacy path.

## Current Limits

- The true user-facing human-input flow still depends on the existing Discord/application integration path.
- Host-side `pytest` still depends on the local Python environment having `logguard` installed; the containerized path avoids that dependency gap.
