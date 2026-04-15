from __future__ import annotations

from collections.abc import Iterable

from orchestrator.core.decision_types import WorkerStageEvent
from orchestrator.core.signal_templates import format_stage_discord_update, format_stage_jira_update


def _build_stage_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    stage: WorkerStageEvent,
    jira_url: str | None,
    run_url: str | None = None,
    pr_url: str | None = None,
    error: str | None = None,
    next_steps: Iterable[str] | None = None,
) -> dict[str, str]:
    return {
        "stage": stage.value,
        "tenant_id": tenant_id,
        "issue_key": issue_key or "",
        "run_id": run_id,
        "jira_message": format_stage_jira_update(
            tenant_id=tenant_id,
            issue_key=issue_key,
            run_id=run_id,
            stage=stage.value,
            jira_url=jira_url,
            pr_url=pr_url,
            error=error,
            next_steps=next_steps or (),
        ),
        "discord_message": format_stage_discord_update(
            tenant_id=tenant_id,
            issue_key=issue_key,
            run_id=run_id,
            stage=stage.value,
            jira_url=jira_url,
            run_url=run_url,
            pr_url=pr_url,
            error=error,
            next_steps=next_steps or (),
        ),
    }


def decision_gate_required_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
    reason: str,
    questions: Iterable[str],
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.DECISION_GATE_REQUIRED,
        jira_url=jira_url,
        run_url=run_url,
        error=reason,
        next_steps=questions,
    )


def run_not_ready_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
    reason: str,
    next_steps: Iterable[str],
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.RUN_NOT_READY,
        jira_url=jira_url,
        run_url=run_url,
        error=reason,
        next_steps=next_steps,
    )


def lock_acquired_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.LOCK_ACQUIRED,
        jira_url=jira_url,
        run_url=run_url,
    )


def plan_posted_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.PLAN_POSTED,
        jira_url=jira_url,
        run_url=run_url,
    )


def repo_setup_ready_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.REPO_SETUP_READY,
        jira_url=jira_url,
        run_url=run_url,
    )


def pr_opened_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
    pr_url: str,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.PR_OPENED,
        jira_url=jira_url,
        run_url=run_url,
        pr_url=pr_url,
    )


def run_failed_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
    error: str,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.RUN_FAILED,
        jira_url=jira_url,
        run_url=run_url,
        error=error,
        next_steps=(
            "Review diagnostics and follow-up issue payload.",
            "Apply fix and move issue back to To Do when ready.",
        ),
    )


def run_requeued_repo_setup_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
    error: str,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.RUN_REQUEUED_REPO_SETUP,
        jira_url=jira_url,
        run_url=run_url,
        error=error,
        next_steps=(
            "A fresh repo-setup attempt will prepare the execution repo before workflow execution resumes.",
            "No workflow agent work was started on this attempt.",
        ),
    )


def run_requeued_capability_mismatch_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
    required_worker_label: str,
    error: str,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.RUN_REQUEUED_CAPABILITY_MISMATCH,
        jira_url=jira_url,
        run_url=run_url,
        error=error,
        next_steps=(
            f"Waiting for {required_worker_label} to pick up this run.",
            "No developer changes were executed on this worker.",
        ),
    )


def run_requeued_stale_snapshot_update(
    *,
    tenant_id: str,
    issue_key: str | None,
    run_id: str,
    jira_url: str | None,
    run_url: str | None = None,
    error: str,
) -> dict[str, str]:
    return _build_stage_update(
        tenant_id=tenant_id,
        issue_key=issue_key,
        run_id=run_id,
        stage=WorkerStageEvent.RUN_REQUEUED_STALE_SNAPSHOT,
        jira_url=jira_url,
        run_url=run_url,
        error=error,
        next_steps=(
            "A fresh run will restart from the latest upstream branch snapshot.",
            "The current run branch was not updated in place.",
        ),
    )
