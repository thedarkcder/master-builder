from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.platform_team_catalog_service import platform_team_catalog_service
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.transitions import ACTIVE_WORKFLOW_STATUSES as WORKFLOW_ACTIVE_STATUSES, is_workflow_terminal
from orchestrator.temporal.team_run_orchestration import start_team_run_workflow_for_run
from orchestrator.storage.models import Run, WebhookDelivery, WorkflowExecution
from orchestrator.storage.run_queue_events import notify_run_enqueued

logger = logging.getLogger(__name__)

RUN_DEDUPE_SCOPE_ISSUE_EXECUTION = "issue_execution"
RUN_DEDUPE_SCOPE_PR_REMEDIATION = "pr_remediation"

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_FOR_INPUT = "waiting_for_input"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

ACTIVE_RUN_STATUSES = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
}
ACTIVE_WORKFLOW_STATUSES = set(WORKFLOW_ACTIVE_STATUSES)
NON_TERMINAL_RUN_STATUSES = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_FOR_INPUT,
    RUN_STATUS_BLOCKED,
}
TERMINAL_RUN_STATUSES = {
    RUN_STATUS_SUCCEEDED,
    RUN_STATUS_FAILED,
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
    workflow_id: str | None = None
    source_workflow_id: str | None = None
    parent_run_id: str | None = None
    entry_mode: str = "fresh"
    entry_stage: str | None = None
    entry_checkpoint_id: str | None = None


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
    if plan is None or (isinstance(plan, dict) and not plan):
        return None
    snapshot = ExecutionSnapshot.load(plan)
    if snapshot is None:
        raise RunStateTransitionError("Unsupported execution snapshot version/shape")
    raw_outcome = snapshot.context.execution_context.get("pre_check_outcome")
    normalized_outcome = str(raw_outcome or "").strip()
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


def _active_workflow_for_issue(session: Session, tenant_id: str, issue_key: str, *, dedupe_scope: str) -> WorkflowExecution | None:
    return session.execute(
        select(WorkflowExecution).where(
            WorkflowExecution.tenant_id == tenant_id,
            WorkflowExecution.issue_key == issue_key,
            WorkflowExecution.dedupe_scope == dedupe_scope,
            WorkflowExecution.status.in_(WORKFLOW_ACTIVE_STATUSES),
        )
    ).scalar_one_or_none()


def _run_for_workflow(session: Session, workflow: WorkflowExecution) -> Run | None:
    active_run_id = str(workflow.active_run_id or "").strip()
    if active_run_id:
        active_run = session.get(Run, active_run_id)
        if active_run is not None:
            return active_run
    return session.execute(
        select(Run)
        .where(Run.workflow_id == workflow.workflow_id)
        .order_by(Run.attempt_number.desc())
        .limit(1)
    ).scalars().first()


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
    orchestration_backend: str | None = None,
) -> dict[str, object] | None:
    snapshot: ExecutionSnapshot
    if bootstrap is not None and bootstrap.plan is not None:
        snapshot = ExecutionSnapshot.load(bootstrap.plan)
        if snapshot is None:
            raise RunStateTransitionError("Bootstrap plan must be a canonical execution snapshot")
    else:
        snapshot = ExecutionSnapshot.empty()
    if normalized_precheck_outcome is not None:
        snapshot.context.execution_context["pre_check_outcome"] = normalized_precheck_outcome
    if not isinstance(snapshot.context.execution_context.get("team_run"), dict):
        snapshot.context.execution_context["team_run"] = platform_team_catalog_service.build_issue_workflow_team_run_snapshot(
            snapshot=snapshot,
            entry_mode=(bootstrap.entry_mode if bootstrap is not None else "fresh"),
            entry_stage=(bootstrap.entry_stage if bootstrap is not None else None),
            max_loops=None,
        )
    normalized_backend = "temporal"
    snapshot.context.execution_context["orchestration_backend"] = normalized_backend
    return snapshot.dump()


def _current_orchestration_backend() -> str:
    normalized = str(get_settings().orchestration_backend or "").strip().lower()
    return normalized or "temporal"


def _temporal_required_for_run(*, bootstrap: RunBootstrap | None = None, run: Run | None = None) -> bool:
    if bootstrap is not None and isinstance(bootstrap.plan, dict):
        snapshot = ExecutionSnapshot.load(bootstrap.plan)
        if snapshot is not None and isinstance(snapshot.context.execution_context.get("team_run"), dict):
            return True
    if run is not None:
        snapshot = ExecutionSnapshot.load(run.plan)
        if snapshot is not None and isinstance(snapshot.context.execution_context.get("team_run"), dict):
            return True
    entry_stage = ""
    if bootstrap is not None:
        entry_stage = str(bootstrap.entry_stage or "").strip().lower()
    elif run is not None:
        entry_stage = str(run.entry_stage or "").strip().lower()
    return entry_stage == "team"


def _set_run_orchestration_backend(*, run: Run, backend: str) -> None:
    snapshot = ExecutionSnapshot.load(run.plan)
    if snapshot is None:
        snapshot = ExecutionSnapshot.empty()
    snapshot.context.execution_context["orchestration_backend"] = str(backend or "").strip().lower() or "legacy"
    run.plan = snapshot.dump()


def _run_orchestration_backend(run: Run | None) -> str | None:
    if run is None:
        return None
    snapshot = ExecutionSnapshot.load(run.plan)
    if snapshot is None:
        return None
    normalized = str(snapshot.context.execution_context.get("orchestration_backend") or "").strip().lower()
    return normalized or None


def _workflow_orchestration_backend(session: Session, workflow: WorkflowExecution) -> str:
    workflow_run = _run_for_workflow(session, workflow)
    inherited = _run_orchestration_backend(workflow_run)
    if inherited:
        return inherited
    return _current_orchestration_backend()


def _maybe_start_temporal_run_workflow(*, session: Session, run: Run) -> None:
    settings = get_settings()
    temporal_required = _temporal_required_for_run(run=run)
    if not temporal_required:
        return
    try:
        start_team_run_workflow_for_run(settings=settings, run=run)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "temporal_team_run_workflow_start_failed run_id=%s workflow_id=%s error=%s",
            run.run_id,
            run.workflow_id,
            exc,
        )
        mark_run_terminal(
            session,
            run_id=str(run.run_id),
            terminal_status=RUN_STATUS_FAILED,
            last_error=f"Temporal team workflow start failed: {exc}",
        )


def _next_attempt_number(session: Session, workflow_id: str) -> int:
    current = session.execute(
        select(func.max(Run.attempt_number)).where(Run.workflow_id == workflow_id)
    ).scalar_one()
    return int(current or 0) + 1


def _resolve_entry_stage(*, bootstrap: RunBootstrap | None) -> str:
    if bootstrap is not None:
        normalized = str(bootstrap.entry_stage or "").strip().lower()
        if normalized:
            return normalized
    return "orchestrated"


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

    bootstrap_workflow_id = str(bootstrap.workflow_id or "").strip() if bootstrap is not None else ""
    if bootstrap_workflow_id:
        return enqueue_attempt_for_workflow(
            session,
            workflow_id=bootstrap_workflow_id,
            bootstrap=bootstrap,
        )

    active_workflow = _active_workflow_for_issue(
        session,
        tenant_id=tenant_id,
        issue_key=issue_key,
        dedupe_scope=normalized_dedupe_scope,
    )
    if active_workflow is not None:
        active_run = _run_for_workflow(session, active_workflow)
        if active_run is None:
            raise RunStateTransitionError(
                f"Workflow {active_workflow.workflow_id} is active but has no attempt rows"
            )
        return EnqueueRunResult(enqueued=False, reason="run_already_active", run=active_run)

    normalized_limit = _coerce_positive_limit(max_concurrent_runs)
    if normalized_limit is not None:
        active_count = _active_run_count_for_tenant(session, tenant_id=tenant_id)
        if active_count >= normalized_limit:
            active_run = _first_active_run_for_tenant(session, tenant_id=tenant_id)
            if active_run is None:
                raise RunStateTransitionError("Concurrency limit reached but no active run was found")
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
        orchestration_backend="temporal" if _temporal_required_for_run(bootstrap=bootstrap) else _current_orchestration_backend(),
    )
    workflow_id = str(uuid4())
    run_id = str(uuid4())
    workflow = WorkflowExecution(
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=bootstrap.branch if bootstrap is not None else None,
        pr_url=bootstrap.pr_url if bootstrap is not None else None,
        dedupe_scope=normalized_dedupe_scope,
        status=RUN_STATUS_QUEUED,
        last_error=None,
        active_run_id=run_id,
        latest_checkpoint_id=bootstrap.entry_checkpoint_id if bootstrap is not None else None,
        source_workflow_id=bootstrap.source_workflow_id if bootstrap is not None else None,
        source_run_id=bootstrap.parent_run_id if bootstrap is not None else None,
        blocked_reason=None,
        created_at=now,
        started_at=None,
        finished_at=None,
        updated_at=now,
    )
    run = Run(
        run_id=run_id,
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=bootstrap.branch if bootstrap is not None else None,
        pr_url=bootstrap.pr_url if bootstrap is not None else None,
        attempt_number=1,
        parent_run_id=bootstrap.parent_run_id if bootstrap is not None else None,
        entry_mode=str(bootstrap.entry_mode or "fresh").strip() if bootstrap is not None else "fresh",
        entry_stage=_resolve_entry_stage(bootstrap=bootstrap),
        entry_checkpoint_id=bootstrap.entry_checkpoint_id if bootstrap is not None else None,
        dedupe_scope=normalized_dedupe_scope,
        plan=initial_plan,
        status=RUN_STATUS_QUEUED,
        last_error=None,
        created_at=now,
        started_at=None,
        last_heartbeat_at=None,
        worker_service_instance_id=None,
        finished_at=None,
    )
    session.add(workflow)
    session.add(run)
    if delivery_id:
        session.add(
            WebhookDelivery(
                tenant_id=tenant_id,
                delivery_id=delivery_id,
                run_id=run_id,
                created_at=now,
            )
        )
    notify_run_enqueued(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        run_id=run_id,
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
                return EnqueueRunResult(enqueued=False, reason="duplicate_delivery", run=deduped_run)
        active_workflow = _active_workflow_for_issue(
            session,
            tenant_id=tenant_id,
            issue_key=issue_key,
            dedupe_scope=normalized_dedupe_scope,
        )
        if active_workflow is not None:
            active_run = _run_for_workflow(session, active_workflow)
            if active_run is None:
                raise RunStateTransitionError(
                    f"Workflow {active_workflow.workflow_id} is active but has no attempt rows"
                )
            return EnqueueRunResult(enqueued=False, reason="run_already_active", run=active_run)
        if normalized_limit is not None:
            active_count = _active_run_count_for_tenant(session, tenant_id=tenant_id)
            if active_count >= normalized_limit:
                active_run = _first_active_run_for_tenant(session, tenant_id=tenant_id)
                if active_run is None:
                    raise RunStateTransitionError("Concurrency limit reached but no active run was found")
                return EnqueueRunResult(
                    enqueued=False,
                    reason="tenant_concurrency_limit_reached",
                    run=active_run,
                )
        raise RunStateTransitionError("Failed to enqueue run due to unknown integrity conflict")
    session.refresh(run)
    _maybe_start_temporal_run_workflow(session=session, run=run)
    return EnqueueRunResult(enqueued=True, reason=None, run=run)


def enqueue_attempt_for_workflow(
    session: Session,
    *,
    workflow_id: str,
    bootstrap: RunBootstrap,
) -> EnqueueRunResult:
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise RunStateTransitionError(f"Workflow not found: {workflow_id}")
    if workflow.status in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
        active_run = _run_for_workflow(session, workflow)
        if active_run is None:
            raise RunStateTransitionError(f"Workflow {workflow_id} is active but has no attempt rows")
        return EnqueueRunResult(enqueued=False, reason="run_already_active", run=active_run)
    if is_workflow_terminal(workflow.status):
        raise RunStateTransitionError(
            f"Workflow {workflow_id} is terminal; create a new workflow execution instead of reusing it"
        )
    now = _now()
    run = Run(
        run_id=str(uuid4()),
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        issue_key=workflow.issue_key,
        issue_summary=workflow.issue_summary,
        issue_description=workflow.issue_description,
        repo_url=workflow.repo_url,
        branch=bootstrap.branch or workflow.branch,
        pr_url=bootstrap.pr_url or workflow.pr_url,
        attempt_number=_next_attempt_number(session, workflow.workflow_id),
        parent_run_id=bootstrap.parent_run_id,
        entry_mode=str(bootstrap.entry_mode or "resume").strip() or "resume",
        entry_stage=_resolve_entry_stage(bootstrap=bootstrap),
        entry_checkpoint_id=bootstrap.entry_checkpoint_id,
        dedupe_scope=workflow.dedupe_scope,
        plan=_build_initial_plan(
            bootstrap=bootstrap,
            normalized_precheck_outcome=None,
            orchestration_backend="temporal" if _temporal_required_for_run(bootstrap=bootstrap) else _workflow_orchestration_backend(session, workflow),
        ),
        status=RUN_STATUS_QUEUED,
        last_error=None,
        created_at=now,
        started_at=None,
        last_heartbeat_at=None,
        worker_service_instance_id=None,
        finished_at=None,
    )
    workflow.status = RUN_STATUS_QUEUED
    workflow.last_error = None
    workflow.active_run_id = run.run_id
    workflow.latest_checkpoint_id = bootstrap.entry_checkpoint_id or workflow.latest_checkpoint_id
    workflow.updated_at = now
    workflow.finished_at = None
    session.add(run)
    notify_run_enqueued(
        session,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        run_id=run.run_id,
        issue_key=workflow.issue_key,
    )
    session.commit()
    session.refresh(run)
    _maybe_start_temporal_run_workflow(session=session, run=run)
    return EnqueueRunResult(enqueued=True, reason=None, run=run)


def mark_run_running(session: Session, *, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise RunStateTransitionError(f"Run not found: {run_id}")
    if run.status == RUN_STATUS_RUNNING:
        return run
    if run.status != RUN_STATUS_QUEUED:
        raise RunStateTransitionError(f"Cannot move run {run_id} to running from status {run.status}")
    workflow = session.get(WorkflowExecution, run.workflow_id)
    if workflow is None:
        raise RunStateTransitionError(f"Workflow not found for run {run_id}")
    now = _now()
    run.status = RUN_STATUS_RUNNING
    run.started_at = now
    workflow.status = RUN_STATUS_RUNNING
    workflow.started_at = workflow.started_at or now
    workflow.updated_at = now
    workflow.active_run_id = run.run_id
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
    if terminal_status not in TERMINAL_RUN_STATUSES | {RUN_STATUS_BLOCKED}:
        raise RunStateTransitionError(f"Invalid terminal status: {terminal_status}")
    run = session.get(Run, run_id)
    if run is None:
        raise RunStateTransitionError(f"Run not found: {run_id}")
    if run.status in TERMINAL_RUN_STATUSES | {RUN_STATUS_BLOCKED}:
        if run.status != terminal_status:
            raise RunStateTransitionError(
                f"Cannot move terminal run {run_id} from {run.status} to {terminal_status}"
            )
        return run
    if run.status not in NON_TERMINAL_RUN_STATUSES:
        raise RunStateTransitionError(
            f"Cannot move run {run_id} to terminal status from {run.status}"
        )
    workflow = session.get(WorkflowExecution, run.workflow_id)
    if workflow is None:
        raise RunStateTransitionError(f"Workflow not found for run {run_id}")
    now = _now()
    run.status = terminal_status
    run.last_error = last_error
    run.started_at = run.started_at or now
    run.finished_at = now
    workflow.last_error = last_error
    workflow.updated_at = now
    workflow.active_run_id = run.run_id
    if terminal_status == RUN_STATUS_BLOCKED:
        workflow.status = RUN_STATUS_BLOCKED
        workflow.blocked_reason = last_error
    else:
        workflow.status = terminal_status
        workflow.finished_at = now
        workflow.blocked_reason = None
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
        raise RunStateTransitionError(f"Cannot cancel run {run_id} from terminal status {run.status}")
    workflow = session.get(WorkflowExecution, run.workflow_id)
    if workflow is None:
        raise RunStateTransitionError(f"Workflow not found for run {run_id}")
    now = _now()
    run.status = RUN_STATUS_CANCELLED
    run.last_error = f"Cancelled by {cancelled_by}"
    run.started_at = run.started_at or now
    run.finished_at = now
    workflow.status = RUN_STATUS_CANCELLED
    workflow.last_error = run.last_error
    workflow.finished_at = now
    workflow.updated_at = now
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
    queued_runs = (
        session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == issue_key,
                Run.status == RUN_STATUS_QUEUED,
                Run.dedupe_scope == normalized_dedupe_scope,
            )
            .order_by(Run.created_at.asc())
        )
        .scalars()
        .all()
    )
    if not queued_runs:
        return []

    now = _now()
    cancellation_reason = f"Cancelled by {cancelled_by}"
    workflow_ids = {run.workflow_id for run in queued_runs}
    workflow_active_counts: dict[str, int] = {}
    for workflow_id in workflow_ids:
        workflow_active_counts[workflow_id] = int(
            session.execute(
                select(func.count(Run.run_id)).where(
                    Run.workflow_id == workflow_id,
                    Run.status.in_(NON_TERMINAL_RUN_STATUSES),
                )
            ).scalar_one()
            or 0
        )

    for run in queued_runs:
        run.status = RUN_STATUS_CANCELLED
        run.last_error = cancellation_reason
        run.started_at = run.started_at or now
        run.finished_at = now
        workflow_active_counts[run.workflow_id] = max(0, workflow_active_counts.get(run.workflow_id, 0) - 1)
        workflow = session.get(WorkflowExecution, run.workflow_id)
        if workflow is None:
            continue
        workflow.last_error = cancellation_reason
        workflow.updated_at = now
        if workflow.active_run_id == run.run_id:
            workflow.active_run_id = None
        if workflow_active_counts.get(run.workflow_id, 0) == 0:
            workflow.status = RUN_STATUS_CANCELLED
            workflow.finished_at = now

    session.commit()
    for run in queued_runs:
        session.refresh(run)
    return queued_runs
