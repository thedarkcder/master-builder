from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.worker_capabilities import (
    parse_worker_capabilities,
    required_worker_capability_for_run,
)
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run, start_run
from orchestrator.storage.models import Run, RunLock, Tenant, TenantRunClaim

logger = logging.getLogger(__name__)


@dataclass
class QueueSelectionResult:
    run: Run | None = None
    tenant: Tenant | None = None
    effective_policy: dict | None = None
    terminal_run: Run | None = None


def coerce_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def running_run_count(session: Session, *, tenant_id: str, running_status: str) -> int:
    return int(
        session.execute(
            select(func.count(Run.run_id)).where(
                Run.tenant_id == tenant_id,
                Run.status == running_status,
            )
        ).scalar_one()
    )


def _is_postgres(session: Session) -> bool:
    bind = session.get_bind()
    return bind is not None and bind.dialect.name == "postgresql"


def _lock_queued_run_for_claim(session: Session, *, run_id: str, queued_status: str) -> Run | None:
    claim_query = select(Run).where(
        Run.run_id == run_id,
        Run.status == queued_status,
    )
    if _is_postgres(session):
        claim_query = claim_query.with_for_update(skip_locked=True)
    return session.execute(claim_query).scalar_one_or_none()


def _lock_tenant_run_claim(session: Session, *, tenant_id: str) -> TenantRunClaim:
    now = datetime.now(timezone.utc)
    claim_row = session.get(TenantRunClaim, tenant_id)
    if claim_row is None:
        try:
            with session.begin_nested():
                claim_row = TenantRunClaim(tenant_id=tenant_id, updated_at=now)
                session.add(claim_row)
                session.flush()
        except IntegrityError:
            pass
    claim_query = select(TenantRunClaim).where(TenantRunClaim.tenant_id == tenant_id)
    if _is_postgres(session):
        claim_query = claim_query.with_for_update()
    claim_row = session.execute(claim_query).scalar_one()
    claim_row.updated_at = now
    return claim_row


def _candidate_selection_details(
    session: Session,
    *,
    candidate: Run,
    failed_status: str,
    allowed_capabilities: set[str],
) -> QueueSelectionResult:
    required_capability = required_worker_capability_for_run(candidate)
    if required_capability is not None and required_capability not in allowed_capabilities:
        logger.info(
            "worker_skipping_run_due_to_capability_mismatch run_id=%s tenant_id=%s issue_key=%s required=%s available=%s",
            candidate.run_id,
            candidate.tenant_id,
            candidate.issue_key,
            required_capability,
            ",".join(sorted(allowed_capabilities)),
        )
        return QueueSelectionResult()

    candidate_tenant = session.get(Tenant, candidate.tenant_id)
    if candidate_tenant is None:
        candidate.status = failed_status
        candidate.last_error = "Tenant not found for queued run"
        candidate.finished_at = datetime.now(timezone.utc)
        session.execute(
            delete(RunLock).where(
                RunLock.tenant_id == candidate.tenant_id,
                RunLock.issue_key == candidate.issue_key,
                RunLock.run_id == candidate.run_id,
            )
        )
        session.commit()
        session.refresh(candidate)
        return QueueSelectionResult(terminal_run=candidate)

    project = resolve_project_for_run(session, run=candidate)
    project_overrides = project.policy_overrides if project is not None else {}
    effective_policy = resolve_effective_policy(
        tenant_policy=candidate_tenant.policy_config,
        project_overrides=project_overrides,
    )
    return QueueSelectionResult(
        run=candidate,
        tenant=candidate_tenant,
        effective_policy=effective_policy,
    )


def select_next_queued_run(
    session: Session,
    *,
    queued_status: str,
    running_status: str,
    failed_status: str,
    worker_capabilities: set[str] | None = None,
) -> QueueSelectionResult:
    allowed_capabilities = parse_worker_capabilities(worker_capabilities or [])
    queued_runs = session.execute(
        select(Run).where(Run.status == queued_status).order_by(Run.created_at.asc())
    ).scalars().all()

    for candidate in queued_runs:
        selection = _candidate_selection_details(
            session,
            candidate=candidate,
            failed_status=failed_status,
            allowed_capabilities=allowed_capabilities,
        )
        if selection.terminal_run is not None:
            return selection
        if selection.run is not None and selection.tenant is not None:
            return selection

    return QueueSelectionResult()


def claim_next_queued_run(
    session: Session,
    *,
    queued_status: str,
    running_status: str,
    failed_status: str,
    worker_service_instance_id: str | None,
    worker_capabilities: set[str] | None = None,
) -> QueueSelectionResult:
    allowed_capabilities = parse_worker_capabilities(worker_capabilities or [])
    candidate_run_ids = session.execute(
        select(Run.run_id).where(Run.status == queued_status).order_by(Run.created_at.asc())
    ).scalars().all()

    for run_id in candidate_run_ids:
        candidate = _lock_queued_run_for_claim(session, run_id=run_id, queued_status=queued_status)
        if candidate is None:
            session.rollback()
            continue

        selection = _candidate_selection_details(
            session,
            candidate=candidate,
            failed_status=failed_status,
            allowed_capabilities=allowed_capabilities,
        )
        if selection.terminal_run is not None:
            return selection
        if selection.run is None or selection.tenant is None:
            session.rollback()
            continue

        effective_policy = selection.effective_policy if isinstance(selection.effective_policy, dict) else {}
        max_concurrent_runs = coerce_positive_int(effective_policy.get("max_concurrent_runs"), default=1)
        _lock_tenant_run_claim(session, tenant_id=selection.tenant.tenant_id)
        current_running = running_run_count(
            session,
            tenant_id=selection.tenant.tenant_id,
            running_status=running_status,
        )
        if current_running >= max_concurrent_runs:
            logger.info(
                "worker_skipping_run_due_to_concurrency_limit tenant_id=%s issue_key=%s running=%s max=%s",
                selection.tenant.tenant_id,
                selection.run.issue_key,
                current_running,
                max_concurrent_runs,
            )
            session.rollback()
            continue

        started_run = start_run(
            session,
            run=selection.run,
            expected_status=queued_status,
            worker_service_instance_id=worker_service_instance_id,
        )
        if started_run is None:
            logger.info(
                "worker_skipping_run_claim_conflict run_id=%s tenant_id=%s issue_key=%s",
                selection.run.run_id,
                selection.run.tenant_id,
                selection.run.issue_key,
            )
            session.rollback()
            continue

        selection.run = started_run
        return selection

    return QueueSelectionResult()
