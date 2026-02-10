from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run
from orchestrator.storage.models import Run, Tenant

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


def select_next_queued_run(
    session: Session,
    *,
    queued_status: str,
    running_status: str,
    failed_status: str,
) -> QueueSelectionResult:
    queued_runs = session.execute(
        select(Run).where(Run.status == queued_status).order_by(Run.created_at.asc())
    ).scalars().all()

    for candidate in queued_runs:
        candidate_tenant = session.get(Tenant, candidate.tenant_id)
        if candidate_tenant is None:
            candidate.status = failed_status
            candidate.last_error = "Tenant not found for queued run"
            candidate.finished_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(candidate)
            return QueueSelectionResult(terminal_run=candidate)

        project = resolve_project_for_run(session, run=candidate)
        project_overrides = project.policy_overrides if project is not None else {}
        effective_policy = resolve_effective_policy(
            tenant_policy=candidate_tenant.policy_config,
            project_overrides=project_overrides,
        )

        max_concurrent_runs = coerce_positive_int(effective_policy.get("max_concurrent_runs"), default=1)
        current_running = running_run_count(
            session,
            tenant_id=candidate_tenant.tenant_id,
            running_status=running_status,
        )
        if current_running >= max_concurrent_runs:
            logger.info(
                "worker_skipping_run_due_to_concurrency_limit tenant_id=%s issue_key=%s running=%s max=%s",
                candidate_tenant.tenant_id,
                candidate.issue_key,
                current_running,
                max_concurrent_runs,
            )
            continue

        return QueueSelectionResult(run=candidate, tenant=candidate_tenant, effective_policy=effective_policy)

    return QueueSelectionResult()
