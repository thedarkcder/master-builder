from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace

from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.agent_observability import record_agent_lifecycle_event
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.core.worker.jira_stage_service import send_stage_update_to_jira as _send_stage_update_to_jira
from orchestrator.core.worker.jira_stage_service import transition_issue_status as _transition_issue_status
from orchestrator.core.worker.process_service import (
    process_claimed_run as _process_claimed_run_impl,
    process_next_queued_run as _process_next_queued_run_impl,
)
from orchestrator.core.worker.queue_selector import claim_next_queued_run
from orchestrator.core.worker.run_health import (
    WorkerRunHeartbeatController,
    worker_service_instance_id_for_mode,
)
from orchestrator.core.worker.run_lifecycle import (
    bind_run_project,
    block_archived_project,
    promote_run_to_running,
    fail_guardrail_violation,
    fail_missing_project_mapping,
    fail_project_repository_checkout,
    fail_project_repository_setup,
    finalize_cancelled_run,
    finalize_workflow_result,
    persist_stage_checkpoint,
    requeue_run_for_repo_setup,
    requeue_workflow_result_for_capability,
    requeue_workflow_result_for_stale_snapshot,
    resolve_project_for_run,
)
from orchestrator.core.worker.stage_events import (
    lock_acquired_update,
    repo_setup_ready_update,
    plan_posted_update,
    pr_opened_update,
    run_failed_update,
    run_requeued_repo_setup_update,
    run_requeued_capability_mismatch_update,
    run_requeued_stale_snapshot_update,
)
from orchestrator.core.worker.workflow_request_service import (
    build_workflow_request_for_run as _build_workflow_request_for_run,
)
from orchestrator.core.workflow.runner import WorkflowRequest, WorkflowRunner
from orchestrator.api.admin.route_helpers import ensure_project_repository_checkout
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.project_repo_checkout import check_run_snapshot_freshness
from orchestrator.tools.project_repo_checkout import cleanup_run_workspaces

logger = logging.getLogger(__name__)

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_DISPATCHING = "dispatching"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_CANCELLED = "cancelled"
TransportActionSender = Callable[..., object]


def _workflow_request_for_run(
    session: Session,
    tenant: Tenant,
    run: Run,
    *,
    project: Project | None,
    effective_policy: dict,
) -> WorkflowRequest:
    return _build_workflow_request_for_run(
        session=session,
        tenant=tenant,
        run=run,
        project=project,
        effective_policy=effective_policy,
        settings=get_settings(),
    )


def process_next_queued_run(session: Session, runner: WorkflowRunner) -> Run | None:
    return process_next_queued_run_with_dependencies(
        session=session,
        runner=runner,
        send_discord_message_fn=send_tenant_discord_message,
    )


def process_next_webhook_job_with_dependencies(
    *,
    session_factory: sessionmaker[Session],
    owner_id: str | None = None,
) -> object | None:
    settings = get_settings()
    resolved_owner_id = str(owner_id or "").strip() or (
        f"worker:{worker_service_instance_id_for_mode(settings=settings, mode='webhooks')}"
    )
    with session_factory() as session:
        return process_next_webhook_job(
            session=session,
            settings=settings,
            owner_id=resolved_owner_id,
        )


def process_claimed_run_with_dependencies(
    *,
    session: Session,
    runner: WorkflowRunner,
    run_id: str,
    claim_id: str,
    send_discord_message_fn: TransportActionSender = send_tenant_discord_message,
) -> Run | None:
    settings = get_settings()
    claimed_run = session.get(Run, run_id)
    if claimed_run is None:
        raise RuntimeError(f"Claimed run {run_id} no longer exists")
    expected_owner = worker_service_instance_id_for_mode(settings=settings, mode="runs")
    expected_claim_id = str(claim_id or "").strip()
    if str(getattr(claimed_run, "status", "") or "").strip().lower() != RUN_STATUS_DISPATCHING:
        raise RuntimeError(
            "Claimed run handoff failed: "
            f"run_id={claimed_run.run_id} status={claimed_run.status} expected_status={RUN_STATUS_DISPATCHING}"
        )
    if str(getattr(claimed_run, "worker_service_instance_id", "") or "").strip() != str(expected_owner or "").strip():
        raise RuntimeError(
            "Claimed run owner mismatch: "
            f"run_id={claimed_run.run_id} current_owner={claimed_run.worker_service_instance_id} "
            f"expected_owner={expected_owner}"
        )
    if str(getattr(claimed_run, "claim_id", "") or "").strip() != expected_claim_id:
        raise RuntimeError(
            "Claimed run claim mismatch: "
            f"run_id={claimed_run.run_id} current_claim_id={claimed_run.claim_id} "
            f"expected_claim_id={expected_claim_id}"
        )
    tenant = session.get(Tenant, claimed_run.tenant_id)
    if tenant is None:
        raise RuntimeError(
            f"Claimed run handoff failed: tenant {claimed_run.tenant_id} missing for run {claimed_run.run_id}"
        )
    result = _process_claimed_run_impl(
        session=session,
        runner=runner,
        settings=settings,
        selection=SimpleNamespace(run=claimed_run, tenant=tenant, terminal_run=None),
        send_discord_message_fn=send_discord_message_fn,
        **_runtime_process_kwargs(session=session, settings=settings),
    )
    return result


def _runtime_process_kwargs(*, session: Session, settings: Settings) -> dict[str, object]:
    def _emit_agent_event(
        *,
        event_type: str,
        tenant_id: str,
        project_id: str | None,
        run_id: str,
        issue_key: str,
        agent_id: str | None,
    ) -> None:
        record_agent_lifecycle_event(
            session=session,
            event_type=event_type,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            issue_key=issue_key,
            agent_id=agent_id,
        )

    return dict(
        logger=logger,
        send_jira_message_fn=_send_stage_update_to_jira,
        resolve_project_for_run_fn=resolve_project_for_run,
        fail_missing_project_mapping_fn=fail_missing_project_mapping,
        block_archived_project_fn=block_archived_project,
        ensure_project_repository_checkout_fn=ensure_project_repository_checkout,
        fail_project_repository_checkout_fn=fail_project_repository_checkout,
        fail_project_repository_setup_fn=fail_project_repository_setup,
        cleanup_run_workspaces_fn=cleanup_run_workspaces,
        build_run_heartbeat_controller_fn=lambda *, run_id, worker_service_instance_id, claim_id, heartbeat_interval_seconds: WorkerRunHeartbeatController(
            database_url=settings.database_url,
            run_id=run_id,
            worker_service_instance_id=worker_service_instance_id,
            claim_id=claim_id,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        ),
        promote_run_to_running_fn=promote_run_to_running,
        bind_run_project_fn=bind_run_project,
        workflow_request_for_run_fn=_workflow_request_for_run,
        fail_guardrail_violation_fn=fail_guardrail_violation,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
        lock_acquired_update_fn=lock_acquired_update,
        repo_setup_ready_update_fn=repo_setup_ready_update,
        plan_posted_update_fn=plan_posted_update,
        pr_opened_update_fn=pr_opened_update,
        run_failed_update_fn=run_failed_update,
        run_requeued_repo_setup_update_fn=run_requeued_repo_setup_update,
        run_requeued_capability_update_fn=run_requeued_capability_mismatch_update,
        run_requeued_stale_snapshot_update_fn=run_requeued_stale_snapshot_update,
        finalize_cancelled_run_fn=finalize_cancelled_run,
        finalize_workflow_result_fn=finalize_workflow_result,
        persist_stage_checkpoint_fn=persist_stage_checkpoint,
        requeue_run_for_repo_setup_fn=requeue_run_for_repo_setup,
        requeue_workflow_result_for_capability_fn=requeue_workflow_result_for_capability,
        requeue_workflow_result_for_stale_snapshot_fn=requeue_workflow_result_for_stale_snapshot,
        check_run_snapshot_freshness_fn=check_run_snapshot_freshness,
        transition_issue_status_fn=_transition_issue_status,
        emit_agent_event_fn=_emit_agent_event,
        resolve_agent_id_fn=lambda: settings.agent_id,
        resolve_worker_service_instance_id_fn=lambda: worker_service_instance_id_for_mode(
            settings=settings,
            mode="runs",
        ),
        run_status_running=RUN_STATUS_RUNNING,
        run_status_failed=RUN_STATUS_FAILED,
        run_status_blocked=RUN_STATUS_BLOCKED,
        run_status_cancelled=RUN_STATUS_CANCELLED,
        run_status_dispatching=RUN_STATUS_DISPATCHING,
    )


def process_next_queued_run_with_dependencies(
    *,
    session: Session,
    runner: WorkflowRunner,
    send_discord_message_fn: TransportActionSender = send_tenant_discord_message,
) -> Run | None:
    settings = get_settings()

    return _process_next_queued_run_impl(
        session=session,
        runner=runner,
        settings_fn=lambda: settings,
        claim_next_queued_run_fn=claim_next_queued_run,
        send_discord_message_fn=send_discord_message_fn,
        run_status_queued=RUN_STATUS_QUEUED,
        **_runtime_process_kwargs(session=session, settings=settings),
    )
