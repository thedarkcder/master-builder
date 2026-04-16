from __future__ import annotations

from enum import Enum


class WorkerStageEvent(str, Enum):
    DECISION_GATE_REQUIRED = "decision_gate_required"
    RUN_NOT_READY = "run_not_ready"
    LOCK_ACQUIRED = "lock_acquired"
    REPO_SETUP_READY = "repo_setup_ready"
    PLAN_POSTED = "plan_posted"
    PR_OPENED = "pr_opened"
    RUN_FAILED = "run_failed"
    RUN_REQUEUED_REPO_SETUP = "run_requeued_repo_setup"
    RUN_REQUEUED_CAPABILITY_MISMATCH = "run_requeued_capability_mismatch"
    RUN_REQUEUED_STALE_SNAPSHOT = "run_requeued_stale_snapshot"
