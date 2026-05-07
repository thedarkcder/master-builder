from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.config import Settings
from orchestrator.core.deployment_host_recovery import fail_stale_running_restore_commands
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.observability.metrics import platform_metrics
from orchestrator.core.runtime.requirements import normalize_runtime_kinds
from orchestrator.core.runs.service import (
    RUN_STATUS_DISPATCHING,
    RUN_STATUS_FAILED,
    RunStateTransitionError,
    mark_run_terminal,
)
from orchestrator.core.worker.execution_service import (
    process_claimed_run_with_dependencies,
)
from orchestrator.core.worker.queue_selector import (
    QueueClaimabilityProbe,
    claim_next_queued_run,
    probe_claimable_queued_run,
)
from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session
from orchestrator.core.worker.capabilities import resolve_worker_capability_context
from orchestrator.core.workflow.runner import WorkflowRunner
from orchestrator.storage.models import Run, WebhookJob, WorkflowExecution

logger = logging.getLogger(__name__)

CHILD_DISPATCH_STUCK_ERROR = "Run child completed without moving the run out of dispatching"


class WorkerDependencyFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class ClaimedRunDispatch:
    run_id: str
    claim_id: str
    worker_service_instance_id: str
    tenant_id: str
    issue_key: str


def process_next_run_once(
    *,
    session_factory: sessionmaker[Session],
    claimed_run_id: str,
    claim_id: str,
) -> object | None:
    class _LazyWorkflowRunner:
        def __init__(self, *, session: Session) -> None:
            self._session = session
            self._runner: WorkflowRunner | None = None

        def run(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            if self._runner is None:
                try:
                    self._runner = build_workflow_runner_for_session(session=self._session)
                except CodexRuntimeError as exc:
                    platform_metrics.record_worker_failure(kind="dependency")
                    raise WorkerDependencyFailure(f"Worker runtime unavailable: {exc}") from exc
            return self._runner.run(*args, **kwargs)

    with session_factory() as session:
        normalized_claimed_run_id = str(claimed_run_id or "").strip()
        normalized_claim_id = str(claim_id or "").strip()
        if not normalized_claimed_run_id:
            raise RuntimeError("Run worker child started without claimed_run_id")
        if not normalized_claim_id:
            raise RuntimeError(f"Claimed run {normalized_claimed_run_id} missing claim_id")
        result = process_claimed_run_with_dependencies(
            session=session,
            runner=_LazyWorkflowRunner(session=session),
            run_id=normalized_claimed_run_id,
            claim_id=normalized_claim_id,
            send_discord_message_fn=send_tenant_discord_message,
        )
        if result is None or isinstance(result, Run):
            return result
        logger.warning(
            "worker_run_child_non_run_result type=%s",
            type(result).__name__,
        )
        return None


def claim_next_run_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    service_instance_id: str,
    ready_runtime_kinds: set[str] | None = None,
) -> ClaimedRunDispatch | None:
    capability_context = resolve_worker_capability_context(
        raw_value=getattr(settings, "worker_capabilities", None),
        source="ORCHESTRATOR_WORKER_CAPABILITIES",
    )
    with session_factory() as session:
        selection = claim_next_queued_run(
            session,
            queued_status="queued",
            running_status="running",
            failed_status="failed",
            worker_service_instance_id=service_instance_id,
            worker_capabilities=set(capability_context.available),
            ready_runtime_kinds=ready_runtime_kinds or set(
                normalize_runtime_kinds(getattr(settings, "worker_runtime_kinds", None))
            ),
            running_stale_timeout_seconds=max(
                60,
                int(getattr(settings, "worker_run_stale_timeout_seconds", 300)),
            ),
        )
        if selection.claimed_run is None:
            return None
        return ClaimedRunDispatch(
            run_id=selection.claimed_run.run_id,
            claim_id=selection.claimed_run.claim_id,
            worker_service_instance_id=selection.claimed_run.worker_service_instance_id,
            tenant_id=selection.claimed_run.run.tenant_id,
            issue_key=selection.claimed_run.run.issue_key,
        )


def reconcile_claimed_run_after_child_exit(
    *,
    session_factory: sessionmaker[Session],
    run_id: str,
    claim_id: str,
    worker_service_instance_id: str,
) -> str | None:
    with session_factory() as session:
        run = session.get(Run, run_id)
        if run is None:
            return None
        status = str(getattr(run, "status", "") or "").strip().lower()
        if status == RUN_STATUS_DISPATCHING:
            workflow = session.get(WorkflowExecution, str(getattr(run, "workflow_id", "") or "").strip())
            if (
                workflow is not None
                and str(getattr(workflow, "orchestration_backend", "") or "").strip().lower() == "temporal"
            ):
                return "temporal_handoff"
            current_claim_id = str(getattr(run, "claim_id", "") or "").strip()
            expected_claim_id = str(claim_id or "").strip()
            if current_claim_id and current_claim_id != expected_claim_id:
                logger.warning(
                    "worker_child_post_exit_ownership_lost run_id=%s status=%s current_claim_id=%s expected_claim_id=%s",
                    run_id,
                    status,
                    current_claim_id,
                    expected_claim_id,
                )
                return "ownership_lost"
            try:
                failed_run = mark_run_terminal(
                    session,
                    run_id=run_id,
                    terminal_status=RUN_STATUS_FAILED,
                    last_error=CHILD_DISPATCH_STUCK_ERROR,
                    expected_worker_service_instance_id=worker_service_instance_id,
                    expected_claim_id=claim_id,
                )
            except RunStateTransitionError as exc:
                if "Claim mismatch while terminalizing run" in str(exc):
                    logger.warning(
                        "worker_child_post_exit_claim_changed run_id=%s expected_claim_id=%s error=%s",
                        run_id,
                        expected_claim_id,
                        exc,
                    )
                    return "ownership_lost"
                raise
            return str(getattr(failed_run, "status", "") or "").strip().lower()
        return status


def has_available_webhook_job_once(
    *,
    session_factory: sessionmaker[Session],
) -> bool:
    now = datetime.now(timezone.utc)
    with session_factory() as session:
        fail_stale_running_restore_commands(session=session, now=now)
        session.commit()
        job_id = session.execute(
            select(WebhookJob.job_id)
            .where(
                WebhookJob.available_at <= now,
                or_(
                    WebhookJob.status == "pending",
                    and_(
                        WebhookJob.status == "processing",
                        WebhookJob.lease_expires_at.is_not(None),
                        WebhookJob.lease_expires_at <= now,
                    ),
                ),
            )
            .order_by(WebhookJob.created_at.asc())
            .limit(1)
        ).scalar_one_or_none()
        return job_id is not None


def probe_claimable_run_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    ready_runtime_kinds: set[str] | None = None,
) -> QueueClaimabilityProbe:
    capability_context = resolve_worker_capability_context(
        raw_value=getattr(settings, "worker_capabilities", None),
        source="ORCHESTRATOR_WORKER_CAPABILITIES",
    )
    with session_factory() as session:
        return probe_claimable_queued_run(
            session,
            queued_status="queued",
            running_status="running",
            worker_capabilities=set(capability_context.available),
            ready_runtime_kinds=ready_runtime_kinds or set(
                normalize_runtime_kinds(getattr(settings, "worker_runtime_kinds", None))
            ),
            running_stale_timeout_seconds=max(
                60,
                int(getattr(settings, "worker_run_stale_timeout_seconds", 300)),
            ),
        )
