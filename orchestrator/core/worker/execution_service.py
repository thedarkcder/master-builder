from __future__ import annotations

import logging
from collections.abc import Callable
from types import SimpleNamespace

from sqlalchemy.orm import Session
from sqlalchemy.orm import sessionmaker

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.observability.agent_observability import record_agent_lifecycle_event
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.integrations.atlassian.links import tenant_jira_issue_url
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.workflow.runtime import build_workflow_runtime
from orchestrator.core.workflow.execution_projection import WorkflowExecutionProjection
from orchestrator.core.workflow.step_runner import (
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
    wait_workflow_step_attempt,
)
from orchestrator.core.workflow.type_catalog import ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION, get_workflow_type
from orchestrator.core.workflow.work_units import run_work_unit
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.core.worker.jira_stage_service import send_stage_update_to_jira as _send_stage_update_to_jira
from orchestrator.core.worker.jira_stage_service import transition_issue_status as _transition_issue_status
from orchestrator.core.worker.process_service import (
    process_claimed_run as _process_claimed_run_impl,
    process_next_queued_run as _process_next_queued_run_impl,
)
from orchestrator.core.worker.queue_selector import claim_next_queued_run
from orchestrator.core.worker.queue_selector import ClaimedRun
from orchestrator.core.worker.run_execution_context import resolve_run_execution_policy_context
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
from orchestrator.storage.models import Project, Run, Tenant, WorkflowExecution
from orchestrator.tools.project_repo_checkout import check_run_snapshot_freshness
from orchestrator.tools.project_repo_checkout import cleanup_run_workspaces
from orchestrator.tools.github_app import github_client_from_tenant_config

logger = logging.getLogger(__name__)

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_DISPATCHING = "dispatching"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_FOR_INPUT = "waiting_for_input"
RUN_STATUS_SUCCEEDED = "succeeded"
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


def _github_installation_token_for_project(
    *,
    session: Session,
    settings: Settings,
    tenant: Tenant,
    project: Project,
) -> str:
    github_config = tenant.github_config or {}
    github_client = github_client_from_tenant_config(
        github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )
    return github_client.get_installation_token()


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


def _run_result_payload(*, workflow: WorkflowExecution, run: Run, claim_id: str | None) -> dict[str, object]:
    return {
        "workflow_id": workflow.workflow_id,
        "run_id": run.run_id,
        "status": str(run.status),
        "issue_key": str(run.issue_key),
        "claim_id": str(claim_id or "").strip() or None,
        "last_error": str(getattr(run, "last_error", "") or "").strip() or None,
    }


def _finish_workflow_step_for_run(
    *,
    lifecycle: WorkflowExecutionProjection,
    step,
    run: Run,
) -> None:  # noqa: ANN001
    result_status = str(run.status or "").strip().lower()
    summary = f"Run attempt {run.run_id} finished with status {result_status}"
    if result_status == RUN_STATUS_SUCCEEDED:
        complete_workflow_step_attempt(lifecycle=lifecycle, step=step, summary=summary)
        return
    if result_status == RUN_STATUS_WAITING_FOR_INPUT:
        wait_workflow_step_attempt(lifecycle=lifecycle, step=step, summary=summary)
        return
    if result_status in {RUN_STATUS_BLOCKED, RUN_STATUS_FAILED, RUN_STATUS_CANCELLED}:
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=step,
            category=f"run_attempt_{result_status}",
            message=run.last_error or summary,
        )
        return
    complete_workflow_step_attempt(lifecycle=lifecycle, step=step, summary=summary)


def _process_claimed_run_impl_with_temporal_projection(
    *,
    session: Session,
    runner: WorkflowRunner,
    settings: Settings,
    workflow: WorkflowExecution,
    claimed_run: Run,
    tenant: Tenant,
    expected_owner: str,
    expected_claim_id: str,
    send_discord_message_fn: TransportActionSender,
) -> Run:
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    lifecycle = WorkflowExecutionProjection(session=session, workflow=workflow, workflow_type=workflow_type)
    step = start_workflow_step_attempt(
        lifecycle=lifecycle,
        run_id=claimed_run.run_id,
        operation_type=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        idempotency_key=f"run-attempt:{claimed_run.run_id}",
        target_system="execution_worker",
        target_ref=claimed_run.run_id,
        summary=f"Execute run attempt {claimed_run.attempt_number}",
    )

    policy_context = resolve_run_execution_policy_context(
        session,
        tenant=tenant,
        run=claimed_run,
    )

    def _execute(_context) -> dict[str, object]:  # noqa: ANN001
        processed = _process_claimed_run_impl(
            session=session,
            runner=runner,
            settings=settings,
            selection=SimpleNamespace(
                claimed_run=ClaimedRun(
                    run=claimed_run,
                    tenant=tenant,
                    project=policy_context.project,
                    effective_policy=policy_context.effective_policy,
                    run_id=claimed_run.run_id,
                    claim_id=expected_claim_id,
                    worker_service_instance_id=expected_owner,
                    status=RUN_STATUS_DISPATCHING,
                ),
                run=claimed_run,
                terminal_run=None,
            ),
            **build_run_process_kwargs(
                session=session,
                settings=settings,
                worker_service_instance_id=expected_owner,
                send_discord_message_fn=send_discord_message_fn,
            ),
        )
        if processed is None:
            raise RuntimeError(f"Execution worker returned no run for workflow_id={workflow.workflow_id}")
        return _run_result_payload(workflow=workflow, run=processed, claim_id=expected_claim_id)

    try:
        result_payload = run_work_unit(
            session,
            operation=step.operation,
            operation_attempt=step.attempt,
            unit_key="run_attempt_execution.runtime_invocation",
            idempotency_key=f"run:{claimed_run.run_id}:runtime_invocation:{step.attempt.attempt_id}",
            input_payload={
                "workflow_id": workflow.workflow_id,
                "run_id": claimed_run.run_id,
                "attempt_number": claimed_run.attempt_number,
                "operation_attempt_id": step.attempt.attempt_id,
                "issue_key": claimed_run.issue_key,
                "worker_service_instance_id": expected_owner,
            },
            execute=_execute,
            serialize=lambda payload: dict(payload),
            deserialize=lambda payload: dict(payload),
        )
        processed_run = session.get(Run, str(result_payload["run_id"]))
        if processed_run is None:
            raise RuntimeError(f"Execution worker result referenced missing run {result_payload['run_id']}")
        _finish_workflow_step_for_run(lifecycle=lifecycle, step=step, run=processed_run)
        from orchestrator.temporal.workflow_engine import notify_temporal_run_result

        notify_temporal_run_result(
            session=session,
            settings=settings,
            workflow=workflow,
            run=processed_run,
        )
        return processed_run
    except Exception as exc:  # noqa: BLE001
        logger.exception("execution_worker_temporal_run_failed workflow_id=%s run_id=%s", workflow.workflow_id, claimed_run.run_id)
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=step,
            category="run_execution_failed",
            message=str(exc),
        )
        raise


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
    workflow = session.get(WorkflowExecution, claimed_run.workflow_id)
    if workflow is None:
        raise RuntimeError(
            f"Claimed run handoff failed: workflow {claimed_run.workflow_id} missing for run {claimed_run.run_id}"
        )
    runtime = build_workflow_runtime(
        session=session,
        settings=settings,
        process_claimed_run_fn=_process_claimed_run_impl,
        build_runner_fn=lambda *, session: runner,
        runtime_kwargs_fn=lambda *, session, settings: build_run_process_kwargs(
            session=session,
            settings=settings,
            send_discord_message_fn=send_discord_message_fn,
        ),
    )
    policy_context = resolve_run_execution_policy_context(
        session,
        tenant=tenant,
        run=claimed_run,
    )
    if str(getattr(workflow, "orchestration_backend", "") or "").strip().lower() == "legacy":
        return _process_claimed_run_impl(
            session=session,
            runner=runner,
            settings=settings,
            selection=SimpleNamespace(
                claimed_run=ClaimedRun(
                    run=claimed_run,
                    tenant=tenant,
                    project=policy_context.project,
                    effective_policy=policy_context.effective_policy,
                    run_id=claimed_run.run_id,
                    claim_id=expected_claim_id,
                    worker_service_instance_id=expected_owner,
                    status=RUN_STATUS_DISPATCHING,
                ),
                run=claimed_run,
                terminal_run=None,
            ),
            **build_run_process_kwargs(
                session=session,
                settings=settings,
                worker_service_instance_id=expected_owner,
                send_discord_message_fn=send_discord_message_fn,
            ),
        )
    runtime.start_execution(
        workflow=workflow,
        run=claimed_run,
        claim_id=expected_claim_id,
    )
    return _process_claimed_run_impl_with_temporal_projection(
        session=session,
        runner=runner,
        settings=settings,
        workflow=workflow,
        claimed_run=claimed_run,
        tenant=tenant,
        expected_owner=expected_owner,
        expected_claim_id=expected_claim_id,
        send_discord_message_fn=send_discord_message_fn,
    )


def build_run_process_kwargs(
    *,
    session: Session,
    settings: Settings,
    worker_service_instance_id: str | None = None,
    send_discord_message_fn: TransportActionSender = send_tenant_discord_message,
) -> dict[str, object]:
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

    def _check_run_snapshot_freshness_with_auth(**kwargs: object):
        tenant_id = str(kwargs.get("tenant_id") or "").strip()
        project = kwargs.get("project")
        if not tenant_id:
            raise ValueError("Snapshot freshness check requires tenant_id")
        if not isinstance(project, Project):
            raise ValueError("Snapshot freshness check requires project")
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise ValueError(f"Snapshot freshness check tenant not found: {tenant_id}")
        token = _github_installation_token_for_project(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
        )
        return check_run_snapshot_freshness(
            base_dir=str(kwargs.get("base_dir") or ""),
            tenant_id=tenant_id,
            project=project,
            start_point_ref=str(kwargs.get("start_point_ref") or ""),
            start_point_sha=str(kwargs.get("start_point_sha") or ""),
            github_installation_token=token,
        )

    return dict(
        logger=logger,
        send_discord_message_fn=send_discord_message_fn,
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
        check_run_snapshot_freshness_fn=_check_run_snapshot_freshness_with_auth,
        transition_issue_status_fn=_transition_issue_status,
        emit_agent_event_fn=_emit_agent_event,
        resolve_agent_id_fn=lambda: settings.agent_id,
        resolve_worker_service_instance_id_fn=lambda: str(worker_service_instance_id or "").strip()
        or worker_service_instance_id_for_mode(
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
        run_status_queued=RUN_STATUS_QUEUED,
        **build_run_process_kwargs(
            session=session,
            settings=settings,
            send_discord_message_fn=send_discord_message_fn,
        ),
    )
