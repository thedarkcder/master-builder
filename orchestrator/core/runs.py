from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.storage.models import Run, RunLock, WebhookDelivery
from orchestrator.storage.run_queue_events import notify_run_enqueued

RUN_DEDUPE_SCOPE_ISSUE_EXECUTION = "issue_execution"
RUN_DEDUPE_SCOPE_PR_REMEDIATION = "pr_remediation"

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_CANCELLED = "cancelled"

ACTIVE_RUN_STATUSES = {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}
TERMINAL_RUN_STATUSES = {
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_FAILED,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_CANCELLED,
}


class RunStateTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class EnqueueRunResult:
    enqueued: bool
    reason: str | None
    run: Run


@dataclass(frozen=True)
class RunBootstrap:
    plan: dict[str, object] | None = None
    branch: str | None = None
    pr_url: str | None = None
    pm_session_id: str | None = None
    dev_session_id: str | None = None
    orchestrated_session_id: str | None = None


def normalize_run_dedupe_scope(raw_scope: object | None) -> str:
    normalized = str(raw_scope or "").strip().lower()
    if normalized == RUN_DEDUPE_SCOPE_PR_REMEDIATION:
        return RUN_DEDUPE_SCOPE_PR_REMEDIATION
    return RUN_DEDUPE_SCOPE_ISSUE_EXECUTION


def _normalize_precheck_outcome(raw_outcome: object | None) -> str | None:
    if not isinstance(raw_outcome, str):
        return None
    normalized_outcome = raw_outcome.strip()
    return normalized_outcome if normalized_outcome else None


def resolve_precheck_outcome_from_plan(plan: object | None) -> str | None:
    if not isinstance(plan, dict):
        return None
    pre_check_payload = plan.get("pre_check")
    if not isinstance(pre_check_payload, dict):
        return None
    raw_outcome = pre_check_payload.get("outcome")
    if not isinstance(raw_outcome, str):
        return None
    normalized_outcome = raw_outcome.strip()
    return normalized_outcome if normalized_outcome else None


def resolve_precheck_outcome_for_enqueue(
    *,
    precheck_outcome: str | None,
    precheck_source_plan: object | None = None,
) -> str | None:
    normalized_outcome = _normalize_precheck_outcome(precheck_outcome)
    if normalized_outcome is not None:
        return normalized_outcome
    return resolve_precheck_outcome_from_plan(precheck_source_plan)


def is_ready_for_agent_precheck(plan: object | None) -> bool:
    return (resolve_precheck_outcome_from_plan(plan) or "").casefold() == "ready_for_agent"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active_run_for_issue(session: Session, tenant_id: str, issue_key: str, *, dedupe_scope: str) -> Run | None:
    active_lock = session.execute(
        select(RunLock).where(
            RunLock.tenant_id == tenant_id,
            RunLock.issue_key == issue_key,
            RunLock.dedupe_scope == dedupe_scope,
        )
    ).scalar_one_or_none()
    if active_lock is not None:
        locked_run = session.get(Run, active_lock.run_id)
        if (
            locked_run is not None
            and locked_run.status in ACTIVE_RUN_STATUSES
            and normalize_run_dedupe_scope(getattr(locked_run, "dedupe_scope", None)) == dedupe_scope
        ):
            return locked_run
        session.delete(active_lock)
        session.flush()

    return session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status.in_(ACTIVE_RUN_STATUSES),
            Run.dedupe_scope == dedupe_scope,
        )
    ).scalar_one_or_none()


def _active_run_count_for_tenant(session: Session, tenant_id: str) -> int:
    return int(
        session.execute(
            select(func.count(Run.run_id)).where(
                Run.tenant_id == tenant_id,
                Run.status.in_(ACTIVE_RUN_STATUSES),
            )
        ).scalar_one()
    )


def _first_active_run_for_tenant(session: Session, tenant_id: str) -> Run | None:
    return (
        session.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(Run.created_at.asc())
        .limit(1)
    )
        .scalars()
        .first()
    )


def _coerce_positive_limit(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RunStateTransitionError(f"Invalid max_concurrent_runs value: {value}") from exc
    return max(1, parsed)


def _build_initial_plan(
    *,
    bootstrap: RunBootstrap | None,
    normalized_precheck_outcome: str | None,
) -> dict[str, object] | None:
    next_plan = dict(bootstrap.plan) if bootstrap is not None and isinstance(bootstrap.plan, dict) else {}
    if normalized_precheck_outcome is not None:
        next_plan["pre_check"] = {"outcome": normalized_precheck_outcome}
    return next_plan or None


def enqueue_run(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    issue_summary: str | None = None,
    issue_description: str | None = None,
    repo_url: str | None = None,
    delivery_id: str | None = None,
    precheck_outcome: str | None = None,
    precheck_source_plan: object | None = None,
    max_concurrent_runs: int | None = None,
    dedupe_scope: str | None = None,
    bootstrap: RunBootstrap | None = None,
) -> EnqueueRunResult:
    normalized_dedupe_scope = normalize_run_dedupe_scope(dedupe_scope)
    if delivery_id:
        existing_delivery = session.get(
            WebhookDelivery,
            {"tenant_id": tenant_id, "delivery_id": delivery_id},
        )
        if existing_delivery is not None:
            run = session.get(Run, existing_delivery.run_id)
            if run is None:
                raise RunStateTransitionError(
                    "Webhook delivery references a missing run; DB integrity is violated"
                )
            return EnqueueRunResult(enqueued=False, reason="duplicate_delivery", run=run)

    active_run = _active_run_for_issue(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
        dedupe_scope=normalized_dedupe_scope,
    )
    if active_run is not None:
        return EnqueueRunResult(enqueued=False, reason="run_already_active", run=active_run)

    normalized_limit = _coerce_positive_limit(max_concurrent_runs)
    if normalized_limit is not None:
        active_count = _active_run_count_for_tenant(session, tenant_id=tenant_id)
        if active_count >= normalized_limit:
            active_run = _first_active_run_for_tenant(session, tenant_id=tenant_id)
            if active_run is None:
                raise RunStateTransitionError(
                    "Concurrency limit reached but no active run was found"
                )
            return EnqueueRunResult(
                enqueued=False,
                reason="tenant_concurrency_limit_reached",
                run=active_run,
            )

    now = _now()
    normalized_precheck_outcome = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )
    initial_plan = _build_initial_plan(
        bootstrap=bootstrap,
        normalized_precheck_outcome=normalized_precheck_outcome,
    )
    run = Run(
        run_id=str(uuid4()),
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=bootstrap.branch if bootstrap is not None else None,
        pr_url=bootstrap.pr_url if bootstrap is not None else None,
        pm_session_id=bootstrap.pm_session_id if bootstrap is not None else None,
        dev_session_id=bootstrap.dev_session_id if bootstrap is not None else None,
        orchestrated_session_id=bootstrap.orchestrated_session_id if bootstrap is not None else None,
        dedupe_scope=normalized_dedupe_scope,
        plan=initial_plan,
        status=RUN_STATUS_QUEUED,
        last_error=None,
        created_at=now,
        started_at=None,
        finished_at=None,
    )
    lock = RunLock(
        tenant_id=tenant_id,
        issue_key=issue_key,
        dedupe_scope=normalized_dedupe_scope,
        run_id=run.run_id,
        locked_at=now,
    )
    session.add(run)
    session.add(lock)
    if delivery_id:
        session.add(
            WebhookDelivery(
                tenant_id=tenant_id,
                delivery_id=delivery_id,
                run_id=run.run_id,
                created_at=now,
            )
        )
    notify_run_enqueued(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run.run_id,
        issue_key=issue_key,
    )

    try:
        session.commit()
    except IntegrityError:
        session.rollback()

        if delivery_id:
            existing_delivery = session.get(
                WebhookDelivery,
                {"tenant_id": tenant_id, "delivery_id": delivery_id},
            )
            if existing_delivery is not None:
                deduped_run = session.get(Run, existing_delivery.run_id)
                if deduped_run is None:
                    raise RunStateTransitionError(
                        "Webhook delivery references a missing run after retry"
                    )
                return EnqueueRunResult(
                    enqueued=False,
                    reason="duplicate_delivery",
                    run=deduped_run,
                )

        active_run = _active_run_for_issue(
            session,
            tenant_id=tenant_id,
            issue_key=issue_key,
            dedupe_scope=normalized_dedupe_scope,
        )
        if active_run is not None:
            return EnqueueRunResult(enqueued=False, reason="run_already_active", run=active_run)

        if normalized_limit is not None:
            active_count = _active_run_count_for_tenant(session, tenant_id=tenant_id)
            if active_count >= normalized_limit:
                limited_run = _first_active_run_for_tenant(session, tenant_id=tenant_id)
                if limited_run is None:
                    raise RunStateTransitionError(
                        "Concurrency limit reached after retry but no active run was found"
                    )
                return EnqueueRunResult(
                    enqueued=False,
                    reason="tenant_concurrency_limit_reached",
                    run=limited_run,
                )

        raise RunStateTransitionError("Failed to enqueue run due to unknown integrity conflict")

    return EnqueueRunResult(enqueued=True, reason=None, run=run)


def mark_run_running(session: Session, *, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise RunStateTransitionError(f"Run not found: {run_id}")

    if run.status == RUN_STATUS_RUNNING:
        return run

    if run.status != RUN_STATUS_QUEUED:
        raise RunStateTransitionError(
            f"Cannot move run {run_id} to running from status {run.status}"
        )

    run.status = RUN_STATUS_RUNNING
    run.started_at = _now()
    session.commit()
    session.refresh(run)
    return run


def mark_run_terminal(
    session: Session,
    *,
    run_id: str,
    terminal_status: str,
    last_error: str | None = None,
) -> Run:
    if terminal_status not in TERMINAL_RUN_STATUSES:
        raise RunStateTransitionError(f"Invalid terminal status: {terminal_status}")

    run = session.get(Run, run_id)
    if run is None:
        raise RunStateTransitionError(f"Run not found: {run_id}")

    if run.status in TERMINAL_RUN_STATUSES:
        if run.status != terminal_status:
            raise RunStateTransitionError(
                f"Cannot move terminal run {run_id} from {run.status} to {terminal_status}"
            )
        return run

    if run.status not in ACTIVE_RUN_STATUSES:
        raise RunStateTransitionError(
            f"Cannot move run {run_id} to terminal status from {run.status}"
        )

    now = _now()
    run.status = terminal_status
    run.last_error = last_error
    if run.started_at is None:
        run.started_at = now
    run.finished_at = now

    session.execute(
        delete(RunLock).where(
            RunLock.tenant_id == run.tenant_id,
            RunLock.issue_key == run.issue_key,
            RunLock.run_id == run.run_id,
        )
    )
    session.commit()
    session.refresh(run)
    return run


def cancel_run(
    session: Session,
    *,
    run_id: str,
    cancelled_by: str,
) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise RunStateTransitionError(f"Run not found: {run_id}")

    if run.status in TERMINAL_RUN_STATUSES:
        raise RunStateTransitionError(
            f"Cannot cancel run {run_id} from terminal status {run.status}"
        )

    now = _now()
    run.status = RUN_STATUS_CANCELLED
    run.last_error = f"Cancelled by {cancelled_by}"
    if run.started_at is None:
        run.started_at = now
    run.finished_at = now

    session.execute(
        delete(RunLock).where(
            RunLock.tenant_id == run.tenant_id,
            RunLock.issue_key == run.issue_key,
            RunLock.run_id == run.run_id,
        )
    )
    session.commit()
    session.refresh(run)
    return run


def cancel_queued_issue_runs(
    session: Session,
    *,
    tenant_id: str,
    issue_key: str,
    cancelled_by: str,
    dedupe_scope: str | None = None,
) -> list[Run]:
    normalized_dedupe_scope = normalize_run_dedupe_scope(dedupe_scope)
    queued_runs = session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status == RUN_STATUS_QUEUED,
            Run.dedupe_scope == normalized_dedupe_scope,
        )
    ).scalars().all()
    if not queued_runs:
        return []

    now = _now()
    cancellation_reason = f"Cancelled by {cancelled_by}"
    cancelled_run_ids = {run.run_id for run in queued_runs}
    for run in queued_runs:
        run.status = RUN_STATUS_CANCELLED
        run.last_error = cancellation_reason
        if run.started_at is None:
            run.started_at = now
        run.finished_at = now

    session.execute(
        delete(RunLock).where(
            RunLock.tenant_id == tenant_id,
            RunLock.issue_key == issue_key,
            RunLock.dedupe_scope == normalized_dedupe_scope,
            RunLock.run_id.in_(cancelled_run_ids),
        )
    )
    session.commit()
    for run in queued_runs:
        session.refresh(run)
    return queued_runs
