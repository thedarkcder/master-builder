from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.storage.models import Run, RunLock, WebhookDelivery

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"

ACTIVE_RUN_STATUSES = {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}
TERMINAL_RUN_STATUSES = {RUN_STATUS_SUCCEEDED, RUN_STATUS_FAILED, RUN_STATUS_BLOCKED}


class RunStateTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class EnqueueRunResult:
    enqueued: bool
    reason: str | None
    run: Run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _active_run_for_issue(session: Session, tenant_id: str, issue_key: str) -> Run | None:
    active_lock = session.get(RunLock, {"tenant_id": tenant_id, "issue_key": issue_key})
    if active_lock is not None:
        return session.get(Run, active_lock.run_id)

    return session.execute(
        select(Run).where(
            Run.tenant_id == tenant_id,
            Run.issue_key == issue_key,
            Run.status.in_(ACTIVE_RUN_STATUSES),
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
    return session.execute(
        select(Run)
        .where(
            Run.tenant_id == tenant_id,
            Run.status.in_(ACTIVE_RUN_STATUSES),
        )
        .order_by(Run.created_at.asc())
    ).scalar_one_or_none()


def _coerce_positive_limit(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RunStateTransitionError(f"Invalid max_concurrent_runs value: {value}") from exc
    return max(1, parsed)


def enqueue_run(
    session: Session,
    *,
    tenant_id: str,
    issue_key: str,
    delivery_id: str | None = None,
    max_concurrent_runs: int | None = None,
) -> EnqueueRunResult:
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

    active_run = _active_run_for_issue(session, tenant_id=tenant_id, issue_key=issue_key)
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
    run = Run(
        run_id=str(uuid4()),
        tenant_id=tenant_id,
        issue_key=issue_key,
        repo_url=None,
        branch=None,
        pr_url=None,
        status=RUN_STATUS_QUEUED,
        last_error=None,
        plan=None,
        created_at=now,
        started_at=None,
        finished_at=None,
    )
    lock = RunLock(tenant_id=tenant_id, issue_key=issue_key, run_id=run.run_id, locked_at=now)
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

        active_run = _active_run_for_issue(session, tenant_id=tenant_id, issue_key=issue_key)
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
