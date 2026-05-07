from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from orchestrator.core.projects.policy import resolve_effective_policy
from orchestrator.core.runtime.requirements import normalize_runtime_kinds, required_runtime_kinds_for_run
from orchestrator.core.worker.capability_normalization import WorkerCapability
from orchestrator.core.worker.capabilities import (
    parse_worker_capabilities,
    required_worker_capability_for_run,
)
from orchestrator.core.worker.run_lifecycle import claim_run_for_dispatch, resolve_project_for_run
from orchestrator.storage.models import Project, Run, Tenant, TenantRunClaim

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QueueSelectionResult:
    run: Run | None = None
    tenant: Tenant | None = None
    project: Project | None = None
    effective_policy: dict | None = None
    terminal_run: Run | None = None


@dataclass(frozen=True)
class ClaimedRun:
    run: Run
    tenant: Tenant
    project: Project | None
    effective_policy: dict
    run_id: str
    claim_id: str
    worker_service_instance_id: str
    status: str


@dataclass(frozen=True)
class QueueClaimResult:
    claimed_run: ClaimedRun | None = None
    terminal_run: Run | None = None


class QueueClaimabilityReason(str, Enum):
    CLAIMABLE = "claimable"
    TERMINAL = "terminal"
    NO_QUEUED_RUNS = "no_queued_runs"
    CAPABILITY_MISMATCH = "capability_mismatch"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    CONCURRENCY_LIMIT = "concurrency_limit"


@dataclass(frozen=True)
class QueueClaimabilityProbe:
    claimable: bool
    reason: QueueClaimabilityReason
    run_id: str | None = None
    tenant_id: str | None = None
    issue_key: str | None = None


@dataclass(frozen=True)
class QueueCandidateEvaluation:
    candidate: Run
    tenant: Tenant | None
    project: Project | None
    effective_policy: dict | None
    capability_compatible: bool
    runtime_ready: bool
    tenant_missing: bool


@dataclass(frozen=True)
class QueueCandidateClaimability:
    reason: QueueClaimabilityReason
    selection: QueueSelectionResult | None = None


@dataclass(frozen=True)
class QueueScanResult:
    selection: QueueSelectionResult
    claimability: QueueCandidateClaimability
    blocked_candidate: Run | None
    saw_capability_mismatch: bool
    saw_runtime_unavailable: bool
    saw_concurrency_limit: bool


def coerce_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _effective_last_seen_expr():  # noqa: ANN202
    return func.coalesce(Run.last_heartbeat_at, Run.started_at, Run.dispatch_claimed_at, Run.created_at)


def running_run_count(
    session: Session,
    *,
    tenant_id: str,
    active_statuses: set[str],
    running_stale_timeout_seconds: int | None = None,
) -> int:
    query = select(func.count(Run.run_id)).where(
        Run.tenant_id == tenant_id,
        Run.status.in_(tuple(sorted(active_statuses))),
    )
    if running_stale_timeout_seconds is not None:
        timeout_seconds = max(60, int(running_stale_timeout_seconds))
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout_seconds)
        query = query.where(_effective_last_seen_expr() > cutoff)
    return int(session.execute(query).scalar_one())


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


def _evaluate_candidate(
    session: Session,
    *,
    candidate: Run,
    allowed_capabilities: set[WorkerCapability],
    ready_runtime_kinds: set[str],
) -> QueueCandidateEvaluation:
    required_capability = required_worker_capability_for_run(candidate)
    if required_capability is not None and required_capability not in allowed_capabilities:
        logger.info(
            "worker_skipping_run_due_to_capability_mismatch run_id=%s tenant_id=%s issue_key=%s required=%s available=%s",
            candidate.run_id,
            candidate.tenant_id,
            candidate.issue_key,
            required_capability.value,
            ",".join(sorted(capability.value for capability in allowed_capabilities)),
        )
        return QueueCandidateEvaluation(
            candidate=candidate,
            tenant=None,
            project=None,
            effective_policy=None,
            capability_compatible=False,
            runtime_ready=True,
            tenant_missing=False,
        )

    required_runtime_kinds = required_runtime_kinds_for_run(candidate)
    if required_runtime_kinds and not required_runtime_kinds.issubset(set(ready_runtime_kinds or set())):
        logger.info(
            "worker_skipping_run_due_to_runtime_unavailable run_id=%s tenant_id=%s issue_key=%s required_runtime_kinds=%s ready_runtime_kinds=%s",
            candidate.run_id,
            candidate.tenant_id,
            candidate.issue_key,
            ",".join(sorted(required_runtime_kinds)),
            ",".join(sorted(ready_runtime_kinds)),
        )
        return QueueCandidateEvaluation(
            candidate=candidate,
            tenant=None,
            project=None,
            effective_policy=None,
            capability_compatible=True,
            runtime_ready=False,
            tenant_missing=False,
        )

    candidate_tenant = session.get(Tenant, candidate.tenant_id)
    if candidate_tenant is None:
        return QueueCandidateEvaluation(
            candidate=candidate,
            tenant=None,
            project=None,
            effective_policy=None,
            capability_compatible=True,
            runtime_ready=True,
            tenant_missing=True,
        )

    project = resolve_project_for_run(session, run=candidate)
    project_overrides = project.policy_overrides if project is not None else {}
    effective_policy = resolve_effective_policy(
        tenant_policy=candidate_tenant.policy_config,
        project_overrides=project_overrides,
    )
    return QueueCandidateEvaluation(
        candidate=candidate,
        tenant=candidate_tenant,
        project=project,
        effective_policy=effective_policy,
        capability_compatible=True,
        runtime_ready=True,
        tenant_missing=False,
    )


def _candidate_selection_details(
    session: Session,
    *,
    candidate: Run,
    failed_status: str,
    allowed_capabilities: set[WorkerCapability],
    ready_runtime_kinds: set[str],
) -> QueueSelectionResult:
    evaluation = _evaluate_candidate(
        session,
        candidate=candidate,
        allowed_capabilities=allowed_capabilities,
        ready_runtime_kinds=ready_runtime_kinds,
    )
    if not evaluation.capability_compatible or not evaluation.runtime_ready:
        return QueueSelectionResult()
    if evaluation.tenant_missing:
        candidate.status = failed_status
        candidate.last_error = "Tenant not found for queued run"
        candidate.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(candidate)
        return QueueSelectionResult(terminal_run=candidate)
    return QueueSelectionResult(
        run=candidate,
        tenant=evaluation.tenant,
        project=evaluation.project,
        effective_policy=evaluation.effective_policy,
    )


def _claimability_for_selection(
    session: Session,
    *,
    selection: QueueSelectionResult,
    active_statuses: set[str],
    lock_tenant_claim: bool,
    running_stale_timeout_seconds: int | None = None,
) -> QueueCandidateClaimability:
    if selection.terminal_run is not None:
        return QueueCandidateClaimability(
            reason=QueueClaimabilityReason.TERMINAL,
            selection=selection,
        )
    if selection.run is None or selection.tenant is None:
        return QueueCandidateClaimability(reason=QueueClaimabilityReason.CAPABILITY_MISMATCH)

    effective_policy = selection.effective_policy if isinstance(selection.effective_policy, dict) else {}
    max_concurrent_runs = coerce_positive_int(effective_policy.get("max_concurrent_runs"), default=1)
    if lock_tenant_claim:
        _lock_tenant_run_claim(session, tenant_id=selection.tenant.tenant_id)
    current_running = running_run_count(
        session,
        tenant_id=selection.tenant.tenant_id,
        active_statuses=active_statuses,
        running_stale_timeout_seconds=running_stale_timeout_seconds,
    )
    if current_running >= max_concurrent_runs:
        return QueueCandidateClaimability(reason=QueueClaimabilityReason.CONCURRENCY_LIMIT)
    return QueueCandidateClaimability(
        reason=QueueClaimabilityReason.CLAIMABLE,
        selection=selection,
    )


def _queued_run_ids(session: Session, *, queued_status: str) -> list[str]:
    return session.execute(
        select(Run.run_id).where(Run.status == queued_status).order_by(Run.created_at.asc())
    ).scalars().all()


def _scan_queued_candidates(
    session: Session,
    *,
    queued_status: str,
    failed_status: str,
    allowed_capabilities: set[WorkerCapability],
    ready_runtime_kinds: set[str],
    active_statuses: set[str] | None = None,
    lock_tenant_claim: bool = False,
    running_stale_timeout_seconds: int | None = None,
    lock_for_claim: bool = False,
    terminalize_missing_tenant: bool = True,
) -> QueueScanResult:
    saw_capability_mismatch = False
    saw_runtime_unavailable = False
    saw_concurrency_limit = False
    blocked_candidate: Run | None = None
    candidate_run_ids = _queued_run_ids(session, queued_status=queued_status)

    for run_id in candidate_run_ids:
        candidate = (
            _lock_queued_run_for_claim(session, run_id=run_id, queued_status=queued_status)
            if lock_for_claim
            else session.get(Run, run_id)
        )
        if candidate is None:
            if lock_for_claim:
                session.rollback()
            continue

        if terminalize_missing_tenant:
            selection = _candidate_selection_details(
                session,
                candidate=candidate,
                failed_status=failed_status,
                allowed_capabilities=allowed_capabilities,
                ready_runtime_kinds=ready_runtime_kinds,
            )
        else:
            evaluation = _evaluate_candidate(
                session,
                candidate=candidate,
                allowed_capabilities=allowed_capabilities,
                ready_runtime_kinds=ready_runtime_kinds,
            )
            if not evaluation.capability_compatible or not evaluation.runtime_ready:
                selection = QueueSelectionResult()
            elif evaluation.tenant_missing:
                selection = QueueSelectionResult(terminal_run=candidate)
            else:
                selection = QueueSelectionResult(
                    run=candidate,
                    tenant=evaluation.tenant,
                    project=evaluation.project,
                    effective_policy=evaluation.effective_policy,
                )
        claimability = (
            _claimability_for_selection(
                session,
                selection=selection,
                active_statuses=active_statuses,
                lock_tenant_claim=lock_tenant_claim,
                running_stale_timeout_seconds=running_stale_timeout_seconds,
            )
            if active_statuses is not None
            else (
                QueueCandidateClaimability(reason=QueueClaimabilityReason.TERMINAL, selection=selection)
                if selection.terminal_run is not None
                else (
                    QueueCandidateClaimability(reason=QueueClaimabilityReason.CLAIMABLE, selection=selection)
                    if selection.run is not None and selection.tenant is not None
                    else QueueCandidateClaimability(reason=QueueClaimabilityReason.CAPABILITY_MISMATCH)
                )
            )
        )
        if claimability.reason == QueueClaimabilityReason.TERMINAL:
            return QueueScanResult(
                selection=selection,
                claimability=claimability,
                blocked_candidate=candidate,
                saw_capability_mismatch=saw_capability_mismatch,
                saw_runtime_unavailable=saw_runtime_unavailable,
                saw_concurrency_limit=saw_concurrency_limit,
            )
        if claimability.reason == QueueClaimabilityReason.CLAIMABLE:
            return QueueScanResult(
                selection=claimability.selection or selection,
                claimability=claimability,
                blocked_candidate=candidate,
                saw_capability_mismatch=saw_capability_mismatch,
                saw_runtime_unavailable=saw_runtime_unavailable,
                saw_concurrency_limit=saw_concurrency_limit,
            )
        if claimability.reason == QueueClaimabilityReason.CONCURRENCY_LIMIT:
            saw_concurrency_limit = True
        elif claimability.reason == QueueClaimabilityReason.CAPABILITY_MISMATCH:
            if not _evaluate_candidate(
                session,
                candidate=candidate,
                allowed_capabilities=allowed_capabilities,
                ready_runtime_kinds=ready_runtime_kinds,
            ).runtime_ready:
                saw_runtime_unavailable = True
            else:
                saw_capability_mismatch = True
        blocked_candidate = blocked_candidate or candidate
        if lock_for_claim:
            session.rollback()

    return QueueScanResult(
        selection=QueueSelectionResult(),
        claimability=QueueCandidateClaimability(reason=QueueClaimabilityReason.NO_QUEUED_RUNS),
        blocked_candidate=blocked_candidate,
        saw_capability_mismatch=saw_capability_mismatch,
        saw_runtime_unavailable=saw_runtime_unavailable,
        saw_concurrency_limit=saw_concurrency_limit,
    )


def select_next_queued_run(
    session: Session,
    *,
    queued_status: str,
    running_status: str,
    failed_status: str,
    worker_capabilities: set[WorkerCapability] | None = None,
    ready_runtime_kinds: set[str] | None = None,
) -> QueueSelectionResult:
    allowed_capabilities = parse_worker_capabilities(worker_capabilities or [])
    return _scan_queued_candidates(
        session,
        queued_status=queued_status,
        failed_status=failed_status,
        allowed_capabilities=allowed_capabilities,
        ready_runtime_kinds=set(normalize_runtime_kinds(ready_runtime_kinds or [])),
    ).selection


def probe_claimable_queued_run(
    session: Session,
    *,
    queued_status: str,
    running_status: str,
    dispatching_status: str = "dispatching",
    worker_capabilities: set[WorkerCapability] | None = None,
    ready_runtime_kinds: set[str] | None = None,
    running_stale_timeout_seconds: int | None = None,
) -> QueueClaimabilityProbe:
    allowed_capabilities = parse_worker_capabilities(worker_capabilities or [])
    scan = _scan_queued_candidates(
        session,
        queued_status=queued_status,
        failed_status=running_status,
        allowed_capabilities=allowed_capabilities,
        ready_runtime_kinds=set(normalize_runtime_kinds(ready_runtime_kinds or [])),
        active_statuses={running_status, dispatching_status},
        lock_tenant_claim=False,
        running_stale_timeout_seconds=running_stale_timeout_seconds,
        terminalize_missing_tenant=False,
    )
    if scan.claimability.reason in {QueueClaimabilityReason.CLAIMABLE, QueueClaimabilityReason.TERMINAL}:
        selected = scan.selection.terminal_run if scan.selection.terminal_run is not None else scan.selection.run
        return QueueClaimabilityProbe(
            claimable=True,
            reason=scan.claimability.reason,
            run_id=getattr(selected, "run_id", None),
            tenant_id=getattr(selected, "tenant_id", None),
            issue_key=getattr(selected, "issue_key", None),
        )
    if scan.saw_concurrency_limit:
        reason = QueueClaimabilityReason.CONCURRENCY_LIMIT
    elif scan.saw_runtime_unavailable:
        reason = QueueClaimabilityReason.RUNTIME_UNAVAILABLE
    elif scan.saw_capability_mismatch:
        reason = QueueClaimabilityReason.CAPABILITY_MISMATCH
    else:
        reason = QueueClaimabilityReason.NO_QUEUED_RUNS
    return QueueClaimabilityProbe(
        claimable=False,
        reason=reason,
        run_id=getattr(scan.blocked_candidate, "run_id", None),
        tenant_id=getattr(scan.blocked_candidate, "tenant_id", None),
        issue_key=getattr(scan.blocked_candidate, "issue_key", None),
    )


def claim_next_queued_run(
    session: Session,
    *,
    queued_status: str,
    running_status: str,
    dispatching_status: str = "dispatching",
    failed_status: str,
    worker_service_instance_id: str | None,
    worker_capabilities: set[WorkerCapability] | None = None,
    ready_runtime_kinds: set[str] | None = None,
    running_stale_timeout_seconds: int | None = None,
) -> QueueClaimResult:
    normalized_owner = str(worker_service_instance_id or "").strip()
    if not normalized_owner:
        raise ValueError("worker_service_instance_id is required to claim a queued run")
    allowed_capabilities = parse_worker_capabilities(worker_capabilities or [])
    scan = _scan_queued_candidates(
        session,
        queued_status=queued_status,
        failed_status=failed_status,
        allowed_capabilities=allowed_capabilities,
        ready_runtime_kinds=set(normalize_runtime_kinds(ready_runtime_kinds or [])),
        active_statuses={running_status, dispatching_status},
        lock_tenant_claim=True,
        running_stale_timeout_seconds=running_stale_timeout_seconds,
        lock_for_claim=True,
    )
    selection = scan.selection
    if scan.claimability.reason == QueueClaimabilityReason.TERMINAL:
        return QueueClaimResult(terminal_run=selection.terminal_run)
    if scan.claimability.reason != QueueClaimabilityReason.CLAIMABLE or selection.run is None or selection.tenant is None:
        if scan.claimability.reason == QueueClaimabilityReason.CONCURRENCY_LIMIT:
            logger.info(
                "worker_skipping_run_due_to_concurrency_limit tenant_id=%s issue_key=%s",
                selection.tenant.tenant_id if selection.tenant is not None else None,
                selection.run.issue_key if selection.run is not None else None,
            )
        return QueueClaimResult()

    dispatching_run = claim_run_for_dispatch(
        session,
        run=selection.run,
        expected_status=queued_status,
        worker_service_instance_id=normalized_owner,
    )
    if dispatching_run is None:
        logger.info(
            "worker_skipping_run_claim_conflict run_id=%s tenant_id=%s issue_key=%s",
            selection.run.run_id,
            selection.run.tenant_id,
            selection.run.issue_key,
        )
        session.rollback()
        return QueueClaimResult()

    logger.info(
        "worker_claimed_run run_id=%s tenant_id=%s issue_key=%s worker_service_instance_id=%s claim_id=%s",
        dispatching_run.run_id,
        dispatching_run.tenant_id,
        dispatching_run.issue_key,
        normalized_owner,
        dispatching_run.claim_id,
    )
    claim_id = str(dispatching_run.claim_id or "").strip()
    if not claim_id:
        raise RuntimeError(f"Claimed run {dispatching_run.run_id} has no claim_id")
    return QueueClaimResult(
        claimed_run=ClaimedRun(
            run=dispatching_run,
            tenant=selection.tenant,
            project=selection.project,
            effective_policy=dict(selection.effective_policy or {}),
            run_id=dispatching_run.run_id,
            claim_id=claim_id,
            worker_service_instance_id=normalized_owner,
            status=str(dispatching_run.status or "").strip(),
        )
    )
