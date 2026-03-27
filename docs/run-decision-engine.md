# Run Decision Engine

This document explains where run queue/start decisions are made and what conditions allow or block a run.

Visual flow (draw.io):
- `docs/run-decision-engine.drawio`

## Source Of Truth

Run enqueue and dedup/concurrency rules:
- `orchestrator/core/runs.py`

Command routing and execution context:
- `orchestrator/api/commands/execution_service.py`
- `orchestrator/api/discord/ingress/service.py`
- `orchestrator/api/discord/ingress/wiring.py`

Discord run control handlers:
- `orchestrator/api/discord/commands/run_controls.py`

Jira webhook run triggers:
- `orchestrator/api/webhooks/jira_ingress.py`

Worker pick/start decisions:
- `orchestrator/core/worker/queue_selector.py`
- `orchestrator/core/worker/execution_service.py`
- `orchestrator/core/worker/decision_gate.py`

Board-ingress qualification and Decision Gate:
- `orchestrator/api/webhooks/jira_webhook_precheck.py`
- `orchestrator/core/decision_clarification_service.py`
- `orchestrator/core/worker/process_service.py`

## Ingress Paths That Can Queue Runs

### Discord commands

`!run <ISSUE_KEY>`:
- Validates issue/project scope.
- Validates issue is executable.
- Calls `enqueue_run(...)`.
- Implemented in `orchestrator/api/discord/commands/run_controls.py`.

`!retry <ISSUE_KEY|RUN_ID>`:
- Finds latest run (or explicit run id).
- Requires status in `{failed, blocked, cancelled}`.
- Validates issue executable and scope.
- Calls `enqueue_run(...)`.
- Implemented in `orchestrator/api/discord/commands/run_controls.py`.

### Jira webhook automation

When webhook context is eligible:
- Applies board-ingress qualification and readiness checks.
- Optional retry mode requires a retryable prior run.
- Calls `enqueue_run(...)`.
- Implemented in `orchestrator/api/webhooks/jira_ingress.py`.

## Enqueue Decision Matrix

All enqueue paths converge in `orchestrator/core/runs.py:enqueue_run`.

Inputs:
- `tenant_id`
- `project_id`
- `issue_key`
- optional `delivery_id`
- optional `max_concurrent_runs`

Outcomes:
- `enqueued=True`: new queued run + lock persisted.
- `enqueued=False, reason=duplicate_delivery`: delivery id already seen.
- `enqueued=False, reason=run_already_active`: queued/running run already exists for this issue.
- `enqueued=False, reason=tenant_concurrency_limit_reached`: tenant active-run count at limit.

Side effects on successful enqueue:
- Creates `Run` with `status=queued`.
- Creates `RunLock` for issue-level mutual exclusion.
- Optionally creates `WebhookDelivery` idempotency record.
- Emits queue notify via `notify_run_enqueued(...)`.

## Worker Start Decision Matrix

Queue polling/selection applies additional start gating beyond enqueue:

From `orchestrator/core/worker/queue_selector.py`:
- Selects oldest eligible queued run.
- Enforces tenant max concurrency against currently running runs.
- Skips queued runs that cannot be started yet due to active limits.

From `orchestrator/core/worker/process_service.py` and `orchestrator/core/worker/decision_gate.py`:
- Before full execution, worker rechecks execution readiness only.
- If the configured ready label is missing, run moves to `blocked` with `run_not_ready` guidance.
- Worker does not create, reopen, or re-evaluate Decision Gate.

## Decision Gate Contract

Decision Gate is a board-ingress qualification workflow, not a run-time gate.

- It runs when an issue is added to the configured board in `To Do`.
- It uses persisted answers, project knowledge, and live Jira issue context to self-resolve first.
- If unresolved, it writes the open questions back to Jira and asks the user.
- When all required answers are resolved, it applies the configured ready label.
- `!run`, `!retry`, and worker start checks only enforce execution readiness; they do not surface Decision Gate questions.

## What Is Not The Run Decision Engine

These are interaction/transport layers, not run decision logic:
- Discord interaction ACK/follow-up route handlers.
- Discord thread/follow-up formatting utilities.
- UI/admin route handlers.

They can trigger commands, but enqueue/start decisions are owned by the files above.
