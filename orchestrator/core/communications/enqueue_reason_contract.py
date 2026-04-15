from __future__ import annotations

_ENQUEUE_REASON_GUIDANCE = {
    "run_already_active": "A run for this issue is already active.",
    "tenant_concurrency_limit_reached": "The tenant concurrency limit is reached; wait for an active run to finish.",
    "duplicate_delivery": "This webhook delivery was already processed.",
    "pr_remediation_attempt_limit_reached": "Automatic PR remediation attempt limit reached for this commit head.",
    "project_not_mapped": "Issue key is not mapped to an active project.",
    "no_retryable_run": "No failed/blocked/cancelled run is available to retry for this issue.",
    "ready_for_agent_backlog": "Issue is ready-for-agent in backlog; move it to To Do to start execution.",
    "issue_in_backlog": "Issue is currently in backlog for the configured board; move it onto the board before running.",
    "issue_not_on_board": "Issue is not present on the configured board; place it on the board before running.",
    "board_gate_check_failed": "Board-location gate check failed; verify Jira OAuth connection/scopes and board configuration.",
    "board_gate_unconfigured": "Run board gating is enabled but no valid board id is configured.",
    "decision_gate_cooldown_active": "Decision Gate was recently required for this issue. Wait for cooldown, then rerun.",
    "decision_gate_required": "Decision Gate is required before execution. Reply with the missing clarifications.",
    "gtd_required": "Good To Do details are incomplete. Add the missing GTD details, then rerun.",
    "execution_blocked": "Execution readiness checks failed. Resolve sync/capability blockers, then rerun.",
    "missing_ready_label": "Issue is missing the configured ready label.",
    "policy_eval_failed": "Pre-run policy evaluation failed. Resolve policy/runtime errors before rerunning.",
}


def _normalize_enqueue_reason(reason: object) -> str:
    return str(getattr(reason, "value", reason) or "").strip()


def enqueue_reason_guidance(reason: object) -> str:
    return _ENQUEUE_REASON_GUIDANCE.get(
        _normalize_enqueue_reason(reason),
        "Run was not queued due to current execution policy.",
    )
