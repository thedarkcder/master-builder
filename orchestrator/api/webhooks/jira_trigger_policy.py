from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Run

RUN_STATUS_BLOCKED = "blocked"
RETRYABLE_RUN_STATUSES = {"failed", "blocked", "cancelled"}


@dataclass(frozen=True)
class JiraTriggerDecision:
    trigger_reason: str
    trigger_mode_skip_reason: str | None = None


@dataclass(frozen=True)
class JiraCooldownBlock:
    run_id: str
    remaining_seconds: int


@dataclass(frozen=True)
class JiraRetryResolution:
    source_run: Run | None
    issue_description: str | None
    missing_retryable_run: bool


def resolve_jira_trigger_decision(
    *,
    comment_command: str | None,
    webhook_event: str | None,
    from_status: str | None,
    to_status: str | None,
    ready_trigger_mode: str,
) -> JiraTriggerDecision:
    trigger_reason = "status_recheck"
    if comment_command == "run":
        trigger_reason = "comment_command_run"
    elif comment_command == "retry":
        trigger_reason = "comment_command_retry"
    elif webhook_event == "issue_created":
        trigger_reason = "issue_created"
    elif (
        to_status is not None
        and from_status is not None
        and from_status.casefold() != to_status.casefold()
    ):
        trigger_reason = "status_transition_to_todo" if to_status.strip().lower() == "to do" else "status_transition_to_ready"
    if trigger_reason == "status_recheck" and ready_trigger_mode == "transition_only":
        return JiraTriggerDecision(
            trigger_reason=trigger_reason,
            trigger_mode_skip_reason="ready_status_recheck_disabled",
        )
    return JiraTriggerDecision(trigger_reason=trigger_reason)


def resolve_decision_gate_cooldown_block(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    cooldown_window: timedelta,
    now: datetime,
) -> JiraCooldownBlock | None:
    blocked_run = session.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status == RUN_STATUS_BLOCKED,
            Run.last_error.is_not(None),
            Run.last_error.like("Decision Gate required:%"),
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if blocked_run is None:
        return None
    blocked_at = blocked_run.finished_at or blocked_run.created_at
    if blocked_at is None:
        return None
    if blocked_at.tzinfo is None:
        blocked_at = blocked_at.replace(tzinfo=timezone.utc)
    remaining_seconds = max(0, int((cooldown_window - (now - blocked_at)).total_seconds()))
    if remaining_seconds <= 0:
        return None
    return JiraCooldownBlock(run_id=blocked_run.run_id, remaining_seconds=remaining_seconds)


def resolve_retry_source(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    comment_command: str | None,
    fallback_issue_description: str | None,
) -> JiraRetryResolution:
    if comment_command != "retry":
        return JiraRetryResolution(
            source_run=None,
            issue_description=fallback_issue_description,
            missing_retryable_run=False,
        )
    run = session.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status.in_(RETRYABLE_RUN_STATUSES),
        )
        .order_by(Run.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if run is None:
        return JiraRetryResolution(
            source_run=None,
            issue_description=fallback_issue_description,
            missing_retryable_run=True,
        )
    return JiraRetryResolution(
        source_run=run,
        issue_description=run.issue_description,
        missing_retryable_run=False,
    )
