from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.core.decision_types import PrecheckOutcome
from orchestrator.core.config import get_settings
from orchestrator.core.run_enqueue_types import EnqueueFailureReason
from orchestrator.core.runtime_requirements import resolve_required_runtime_kinds_for_workflow
from orchestrator.core.worker_capability_normalization import parse_worker_capability
from orchestrator.core.workflow_attempt_factory import (
    build_run_attempt,
    build_workflow_execution_for_attempt,
)
from orchestrator.core.workflow_execution_lifecycle import (
    apply_execution_for_cancelled_attempt,
    apply_execution_for_new_attempt,
    apply_execution_for_run_started,
    apply_execution_for_run_terminal,
)
from orchestrator.core.workflow_type_catalog import get_workflow_type
from orchestrator.core.workflow.execution_snapshot import (
    ExecutionSnapshot,
    load_parsed_trigger_context_from_plan,
)
from orchestrator.core.workflow.trigger_context import GithubPrRemediationTriggerContext
from orchestrator.core.workflow.transitions import ACTIVE_WORKFLOW_STATUSES as WORKFLOW_ACTIVE_STATUSES, is_workflow_terminal
from orchestrator.storage.models import Run, WebhookDelivery, WorkflowExecution
from orchestrator.storage.run_queue_events import notify_run_enqueued

RUN_DEDUPE_SCOPE_ISSUE_EXECUTION = "issue_execution"
RUN_DEDUPE_SCOPE_PR_REMEDIATION = "pr_remediation"

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_DISPATCHING = "dispatching"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_FOR_INPUT = "waiting_for_input"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

ACTIVE_RUN_STATUSES = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_DISPATCHING,
    RUN_STATUS_RUNNING,
}
ACTIVE_WORKFLOW_STATUSES = set(WORKFLOW_ACTIVE_STATUSES)
NON_TERMINAL_RUN_STATUSES = {
    RUN_STATUS_QUEUED,
    RUN_STATUS_DISPATCHING,
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
    reason: EnqueueFailureReason | None
    run: Run


@dataclass(frozen=True)
class RunBootstrap:
    plan: dict[str, object] | None = None
    branch: str | None = None
    pr_url: str | None = None
    workflow_id: str | None = None
    parent_run_id: str | None = None
    entry_mode: str = "fresh"
    entry_stage: str | None = None
    entry_checkpoint_id: str | None = None
    precheck_outcome: str | None = None
    required_worker_capability: str | None = None
    required_runtime_kinds_json: list[str] | None = None


def normalize_run_dedupe_scope(raw_scope: object | None) -> str:
    normalized = str(raw_scope or "").strip().lower()
    if normalized == RUN_DEDUPE_SCOPE_PR_REMEDIATION:
        return RUN_DEDUPE_SCOPE_PR_REMEDIATION
    return RUN_DEDUPE_SCOPE_ISSUE_EXECUTION


def _normalize_precheck_outcome(raw_outcome: object | None) -> str | None:
    parsed_outcome = PrecheckOutcome.parse(raw_outcome)
    return parsed_outcome.value if parsed_outcome is not None else None


def resolve_precheck_outcome_from_plan(plan: object | None) -> str | None:
    if plan is None or (isinstance(plan, dict) and not plan):
        return None
    snapshot = ExecutionSnapshot.load(plan)
    if snapshot is None:
        raise RunStateTransitionError("Unsupported execution snapshot version/shape")
    raw_outcome = snapshot.context.execution_context.get("pre_check_outcome")
    parsed_outcome = PrecheckOutcome.parse(raw_outcome)
    return parsed_outcome.value if parsed_outcome is not None else None


def _normalize_required_worker_capability(raw_capability: object | None) -> str | None:
    parsed_capability = parse_worker_capability(raw_capability)
    return parsed_capability.value if parsed_capability is not None else None


def resolve_required_worker_capability_from_plan(plan: object | None) -> str | None:
    if plan is None or (isinstance(plan, dict) and not plan):
        return None
    snapshot = ExecutionSnapshot.load(plan)
    if snapshot is None:
        raise RunStateTransitionError("Unsupported execution snapshot version/shape")
    return _normalize_required_worker_capability(snapshot.workflow.requeue_target)


def resolve_pr_url_from_plan(plan: object | None) -> str | None:
    trigger_context = load_parsed_trigger_context_from_plan(plan)
    normalized_pr_url = str(getattr(trigger_context, "pr_url", "") or "").strip()
    return normalized_pr_url or None


def resolve_pr_url_for_enqueue(
    *,
    pr_url: str | None = None,
    pr_url_source_plan: object | None = None,
) -> str | None:
    normalized_pr_url = str(pr_url or "").strip()
    if normalized_pr_url:
        return normalized_pr_url
    return resolve_pr_url_from_plan(pr_url_source_plan)


def resolve_required_worker_capability_for_enqueue(
    *,
    required_worker_capability: str | None = None,
    required_worker_capability_source_plan: object | None = None,
) -> str | None:
    normalized_capability = _normalize_required_worker_capability(required_worker_capability)
    if normalized_capability is not None:
        return normalized_capability
    return resolve_required_worker_capability_from_plan(required_worker_capability_source_plan)


def resolve_precheck_outcome_for_enqueue(
    *,
    precheck_outcome: str | None,
    precheck_source_plan: object | None = None,
) -> str | None:
    normalized_outcome = _normalize_precheck_outcome(precheck_outcome)
    if normalized_outcome is not None:
        return normalized_outcome
    return resolve_precheck_outcome_from_plan(precheck_source_plan)


def is_pr_remediation_run(
    *,
    run_plan: object | None,
) -> bool:
    trigger_context = load_parsed_trigger_context_from_plan(run_plan)
    return isinstance(trigger_context, GithubPrRemediationTriggerContext)


def resolve_enqueue_precheck_outcome(
    *,
    source: str,
    precheck_outcome: str | None = None,
    precheck_source_plan: object | None = None,
) -> str | None:
    _ = source
    if is_pr_remediation_run(
        run_plan=precheck_source_plan,
    ):
        return PrecheckOutcome.READY_FOR_AGENT.value
    return resolve_precheck_outcome_for_enqueue(
        precheck_outcome=precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )


def require_ready_for_agent_enqueue(
    *,
    source: str,
    precheck_outcome: str | None = None,
    precheck_source_plan: object | None = None,
) -> str:
    resolved_outcome = resolve_enqueue_precheck_outcome(
        source=source,
        precheck_outcome=precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )
    parsed_outcome = PrecheckOutcome.parse(resolved_outcome)
    if parsed_outcome is not PrecheckOutcome.READY_FOR_AGENT:
        raise RunStateTransitionError(
            "Run enqueue rejected: pre_check_outcome must be 'ready_for_agent' before queueing"
        )
    return parsed_outcome.value


def is_ready_for_agent_precheck(plan: object | None) -> bool:
    return PrecheckOutcome.parse(resolve_precheck_outcome_from_plan(plan)) is PrecheckOutcome.READY_FOR_AGENT


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
    return snapshot.dump()


def _run_bootstrap_required_worker_capability(bootstrap: RunBootstrap | None) -> str | None:
    if bootstrap is None:
        return None
    return resolve_required_worker_capability_for_enqueue(
        required_worker_capability=bootstrap.required_worker_capability,
        required_worker_capability_source_plan=bootstrap.plan,
    )


def _run_bootstrap_required_runtime_kinds(bootstrap: RunBootstrap | None) -> list[str] | None:
    if bootstrap is None:
        return None
    raw_value = getattr(bootstrap, "required_runtime_kinds_json", None)
    if isinstance(raw_value, list):
        return [str(item).strip().lower() for item in raw_value if str(item).strip()]
    return None


def _resolve_required_runtime_kinds(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    bootstrap: RunBootstrap | None,
) -> list[str]:
    bootstrap_runtime_kinds = _run_bootstrap_required_runtime_kinds(bootstrap)
    if bootstrap_runtime_kinds is not None:
        return bootstrap_runtime_kinds
    settings = get_settings()
    return resolve_required_runtime_kinds_for_workflow(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
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
    required_worker_capability: str | None = None,
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
            return EnqueueRunResult(
                enqueued=False,
                reason=EnqueueFailureReason.DUPLICATE_DELIVERY,
                run=run,
            )

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
        return EnqueueRunResult(
            enqueued=False,
            reason=EnqueueFailureReason.RUN_ALREADY_ACTIVE,
            run=active_run,
        )

    normalized_limit = _coerce_positive_limit(max_concurrent_runs)
    if normalized_limit is not None:
        active_count = _active_run_count_for_tenant(session, tenant_id=tenant_id)
        if active_count >= normalized_limit:
            active_run = _first_active_run_for_tenant(session, tenant_id=tenant_id)
            if active_run is None:
                raise RunStateTransitionError("Concurrency limit reached but no active run was found")
            return EnqueueRunResult(
                enqueued=False,
                reason=EnqueueFailureReason.TENANT_CONCURRENCY_LIMIT_REACHED,
                run=active_run,
            )

    now = _now()
    normalized_precheck_outcome = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )
    require_ready_for_agent_enqueue(
        source="enqueue_run",
        precheck_outcome=normalized_precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )
    normalized_required_worker_capability = resolve_required_worker_capability_for_enqueue(
        required_worker_capability=required_worker_capability,
        required_worker_capability_source_plan=(
            bootstrap.plan if bootstrap is not None and isinstance(bootstrap.plan, dict) else precheck_source_plan
        ),
    )
    normalized_pr_url = resolve_pr_url_for_enqueue(
        pr_url=bootstrap.pr_url if bootstrap is not None else None,
        pr_url_source_plan=(
            bootstrap.plan if bootstrap is not None and isinstance(bootstrap.plan, dict) else precheck_source_plan
        ),
    )
    initial_plan = _build_initial_plan(
        bootstrap=bootstrap,
        normalized_precheck_outcome=normalized_precheck_outcome,
    )
    normalized_required_runtime_kinds = _resolve_required_runtime_kinds(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        bootstrap=bootstrap,
    )
    workflow_type = get_workflow_type(session, workflow_type_key="issue_execution")
    orchestration_backend = str(workflow_type.orchestration_backend).strip().lower()
    workflow_id = str(uuid4())
    run_id = str(uuid4())
    workflow = build_workflow_execution_for_attempt(
        workflow_id=workflow_id,
        workflow_type_key="issue_execution",
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=bootstrap.branch if bootstrap is not None else None,
        pr_url=normalized_pr_url,
        orchestration_backend=orchestration_backend,
        dedupe_scope=normalized_dedupe_scope,
        status=RUN_STATUS_QUEUED,
        active_run_id=run_id,
        latest_checkpoint_id=bootstrap.entry_checkpoint_id if bootstrap is not None else None,
        source_workflow_id=None,
        source_run_id=bootstrap.parent_run_id if bootstrap is not None else None,
        created_at=now,
        updated_at=now,
    )
    run = build_run_attempt(
        run_id=run_id,
        workflow_id=workflow_id,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        branch=bootstrap.branch if bootstrap is not None else None,
        pr_url=normalized_pr_url,
        attempt_number=1,
        parent_run_id=bootstrap.parent_run_id if bootstrap is not None else None,
        entry_mode=str(bootstrap.entry_mode or "fresh").strip() if bootstrap is not None else "fresh",
        entry_stage=_resolve_entry_stage(bootstrap=bootstrap),
        entry_checkpoint_id=bootstrap.entry_checkpoint_id if bootstrap is not None else None,
        dedupe_scope=normalized_dedupe_scope,
        plan=initial_plan,
        status=RUN_STATUS_QUEUED,
        pre_check_outcome=normalized_precheck_outcome,
        required_worker_capability=normalized_required_worker_capability,
        required_runtime_kinds_json=normalized_required_runtime_kinds,
        created_at=now,
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
                return EnqueueRunResult(
                    enqueued=False,
                    reason=EnqueueFailureReason.DUPLICATE_DELIVERY,
                    run=deduped_run,
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
            return EnqueueRunResult(
                enqueued=False,
                reason=EnqueueFailureReason.RUN_ALREADY_ACTIVE,
                run=active_run,
            )
        if normalized_limit is not None:
            active_count = _active_run_count_for_tenant(session, tenant_id=tenant_id)
            if active_count >= normalized_limit:
                active_run = _first_active_run_for_tenant(session, tenant_id=tenant_id)
                if active_run is None:
                    raise RunStateTransitionError("Concurrency limit reached but no active run was found")
                return EnqueueRunResult(
                    enqueued=False,
                    reason=EnqueueFailureReason.TENANT_CONCURRENCY_LIMIT_REACHED,
                    run=active_run,
                )
        raise RunStateTransitionError("Failed to enqueue run due to unknown integrity conflict")
    session.refresh(run)
    return EnqueueRunResult(enqueued=True, reason=None, run=run)


def enqueue_attempt_for_workflow(
    session: Session,
    *,
    workflow_id: str,
    bootstrap: RunBootstrap,
) -> EnqueueRunResult:
    return _enqueue_attempt_for_workflow(
        session,
        workflow_id=workflow_id,
        bootstrap=bootstrap,
        commit=True,
    )


def enqueue_attempt_for_workflow_uncommitted(
    session: Session,
    *,
    workflow_id: str,
    bootstrap: RunBootstrap,
) -> EnqueueRunResult:
    return _enqueue_attempt_for_workflow(
        session,
        workflow_id=workflow_id,
        bootstrap=bootstrap,
        commit=False,
    )


def _enqueue_attempt_for_workflow(
    session: Session,
    *,
    workflow_id: str,
    bootstrap: RunBootstrap,
    commit: bool,
) -> EnqueueRunResult:
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise RunStateTransitionError(f"Workflow not found: {workflow_id}")
    if workflow.status in {RUN_STATUS_QUEUED, RUN_STATUS_RUNNING}:
        active_run = _run_for_workflow(session, workflow)
        if active_run is None:
            raise RunStateTransitionError(f"Workflow {workflow_id} is active but has no attempt rows")
        return EnqueueRunResult(
            enqueued=False,
            reason=EnqueueFailureReason.RUN_ALREADY_ACTIVE,
            run=active_run,
        )
    if is_workflow_terminal(workflow.status) and str(bootstrap.entry_mode or "").strip().lower() != "resume":
        raise RunStateTransitionError(
            f"Workflow {workflow_id} is terminal; create a new workflow execution instead of reusing it"
        )
    require_ready_for_agent_enqueue(
        source="workflow_attempt",
        precheck_outcome=bootstrap.precheck_outcome,
        precheck_source_plan=bootstrap.plan,
    )
    normalized_precheck_outcome = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=bootstrap.precheck_outcome,
        precheck_source_plan=bootstrap.plan,
    )
    normalized_required_worker_capability = _run_bootstrap_required_worker_capability(bootstrap)
    normalized_pr_url = resolve_pr_url_for_enqueue(
        pr_url=bootstrap.pr_url or workflow.pr_url,
        pr_url_source_plan=bootstrap.plan,
    )
    normalized_required_runtime_kinds = _resolve_required_runtime_kinds(
        session=session,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        bootstrap=bootstrap,
    )
    now = _now()
    run = build_run_attempt(
        run_id=str(uuid4()),
        workflow_id=workflow.workflow_id,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        issue_key=workflow.issue_key,
        issue_summary=workflow.issue_summary,
        issue_description=workflow.issue_description,
        repo_url=workflow.repo_url,
        branch=bootstrap.branch or workflow.branch,
        pr_url=normalized_pr_url,
        attempt_number=_next_attempt_number(session, workflow.workflow_id),
        parent_run_id=bootstrap.parent_run_id,
        entry_mode=str(bootstrap.entry_mode or "resume").strip() or "resume",
        entry_stage=_resolve_entry_stage(bootstrap=bootstrap),
        entry_checkpoint_id=bootstrap.entry_checkpoint_id,
        dedupe_scope=workflow.dedupe_scope,
        plan=dict(bootstrap.plan) if isinstance(bootstrap.plan, dict) else None,
        status=RUN_STATUS_QUEUED,
        pre_check_outcome=normalized_precheck_outcome,
        required_worker_capability=normalized_required_worker_capability,
        required_runtime_kinds_json=normalized_required_runtime_kinds,
        created_at=now,
    )
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    orchestration_backend = str(workflow_type.orchestration_backend).strip().lower()
    apply_execution_for_new_attempt(
        session=session,
        run=run,
        latest_checkpoint_id=bootstrap.entry_checkpoint_id or workflow.latest_checkpoint_id,
        orchestration_backend=orchestration_backend,
        now=now,
    )
    session.add(run)
    notify_run_enqueued(
        session,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        run_id=run.run_id,
        issue_key=workflow.issue_key,
    )
    if commit:
        session.commit()
        session.refresh(run)
    else:
        session.flush()
    return EnqueueRunResult(enqueued=True, reason=None, run=run)


def mark_run_running(session: Session, *, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise RunStateTransitionError(f"Run not found: {run_id}")
    if run.status == RUN_STATUS_RUNNING:
        return run
    if run.status not in {RUN_STATUS_QUEUED, RUN_STATUS_DISPATCHING}:
        raise RunStateTransitionError(f"Cannot move run {run_id} to running from status {run.status}")
    now = _now()
    run.status = RUN_STATUS_RUNNING
    run.claim_id = None
    run.dispatch_claimed_at = None
    run.started_at = now
    run.last_heartbeat_at = now
    apply_execution_for_run_started(
        session=session,
        run=run,
        now=now,
    )
    session.commit()
    session.refresh(run)
    return run


def mark_run_terminal(
    session: Session,
    *,
    run_id: str,
    terminal_status: str,
    last_error: str | None = None,
    expected_claim_id: str | None = None,
) -> Run:
    if terminal_status not in TERMINAL_RUN_STATUSES | {RUN_STATUS_BLOCKED}:
        raise RunStateTransitionError(f"Invalid terminal status: {terminal_status}")
    run = session.get(Run, run_id)
    if run is None:
        raise RunStateTransitionError(f"Run not found: {run_id}")
    normalized_expected_claim_id = str(expected_claim_id or "").strip() or None
    if normalized_expected_claim_id is not None and str(run.claim_id or "").strip() != normalized_expected_claim_id:
        raise RunStateTransitionError(
            f"Claim mismatch while terminalizing run {run_id}: expected {normalized_expected_claim_id} got {run.claim_id}"
        )
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
    now = _now()
    run.status = terminal_status
    run.last_error = last_error
    run.claim_id = None
    run.dispatch_claimed_at = None
    run.started_at = run.started_at or now
    run.finished_at = now
    run.last_heartbeat_at = None
    run.worker_service_instance_id = None
    apply_execution_for_run_terminal(
        session=session,
        run=run,
        now=now,
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
        raise RunStateTransitionError(f"Cannot cancel run {run_id} from terminal status {run.status}")
    now = _now()
    run.status = RUN_STATUS_CANCELLED
    run.last_error = f"Cancelled by {cancelled_by}"
    run.claim_id = None
    run.dispatch_claimed_at = None
    run.started_at = run.started_at or now
    run.finished_at = now
    run.last_heartbeat_at = None
    run.worker_service_instance_id = None
    apply_execution_for_run_terminal(
        session=session,
        run=run,
        now=now,
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
        run.claim_id = None
        run.dispatch_claimed_at = None
        run.started_at = run.started_at or now
        run.finished_at = now
        run.last_heartbeat_at = None
        run.worker_service_instance_id = None
        workflow_active_counts[run.workflow_id] = max(0, workflow_active_counts.get(run.workflow_id, 0) - 1)
        apply_execution_for_cancelled_attempt(
            session=session,
            run=run,
            cancellation_reason=cancellation_reason,
            remaining_active_runs=workflow_active_counts.get(run.workflow_id, 0),
            now=now,
        )

    session.commit()
    for run in queued_runs:
        session.refresh(run)
    return queued_runs
