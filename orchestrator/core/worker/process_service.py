from __future__ import annotations

import json
from uuid import uuid4

from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import RUN_STATUS_WAITING_FOR_INPUT
from orchestrator.core.run_logs import record_run_log_event
from orchestrator.core.worker.finalization import CompletionTailExecutor, WorkflowFinalizer
from orchestrator.core.worker_capabilities import (
    normalize_worker_capability,
    parse_worker_capabilities,
    worker_label_for_capability,
)
from orchestrator.core.worker.stage_notifier import RunStageNotifier
from orchestrator.core.worker_workspace import resolve_worker_workspace_key


def _emit_queue_wait_metric(*, session, run, project_id: str | None, agent_id: str) -> None:  # noqa: ANN001
    if run.started_at is None or run.created_at is None:
        return
    wait_ms = max(0, int((run.started_at - run.created_at).total_seconds() * 1000))
    record_run_log_event(
        session=session,
        tenant_id=run.tenant_id,
        project_id=project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        agent_id=agent_id,
        invocation_id=uuid4().hex,
        channel="worker",
        command="workflow.queue_wait",
        working_dir=None,
        stage="telemetry",
        attempt=None,
        stream="system",
        message=json.dumps(
            {
                "event_kind": "queue_wait",
                "queue_wait_ms": wait_ms,
                "created_at": run.created_at.isoformat(),
                "started_at": run.started_at.isoformat(),
            },
            sort_keys=True,
        ),
    )
    session.commit()


def process_next_queued_run(
    *,
    session,
    runner,
    logger,
    settings_fn,
    claim_next_queued_run_fn,
    apply_decision_gate_fn,
    send_discord_message_fn,
    send_jira_message_fn,
    ask_reply_components_fn,
    resolve_project_for_run_fn,
    fail_missing_project_mapping_fn,
    block_archived_project_fn,
    ensure_project_repository_checkout_fn,
    fail_project_repository_checkout_fn,
    cleanup_run_workspaces_fn,
    build_run_heartbeat_controller_fn,
    bind_run_project_fn,
    workflow_request_for_run_fn,
    fail_guardrail_violation_fn,
    tenant_jira_issue_url_fn,
    lock_acquired_update_fn,
    plan_posted_update_fn,
    pr_opened_update_fn,
    run_failed_update_fn,
    run_requeued_capability_update_fn,
    run_requeued_stale_snapshot_update_fn,
    finalize_cancelled_run_fn,
    finalize_workflow_result_fn,
    persist_stage_checkpoint_fn,
    requeue_workflow_result_for_capability_fn,
    requeue_workflow_result_for_stale_snapshot_fn,
    check_run_snapshot_freshness_fn,
    transition_issue_status_fn,
    emit_agent_event_fn,
    resolve_agent_id_fn,
    resolve_worker_service_instance_id_fn,
    run_status_queued: str,
    run_status_running: str,
    run_status_failed: str,
    run_status_blocked: str,
    run_status_cancelled: str,
):  # noqa: ANN001
    settings = settings_fn()
    worker_workspace_key = resolve_worker_workspace_key(settings=settings)
    agent_id = resolve_agent_id_fn()
    worker_service_instance_id = resolve_worker_service_instance_id_fn()
    selection = claim_next_queued_run_fn(
        session,
        queued_status=run_status_queued,
        running_status=run_status_running,
        failed_status=run_status_failed,
        worker_service_instance_id=worker_service_instance_id,
        worker_capabilities=parse_worker_capabilities(getattr(settings, "worker_capabilities", "")),
    )
    if selection.terminal_run is not None:
        return selection.terminal_run
    if selection.run is None or selection.tenant is None:
        return None
    run = selection.run
    tenant = selection.tenant

    emit_agent_event_fn(
        event_type="ISSUE_ASSIGNED",
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        agent_id=agent_id,
    )

    try:
        decision_gate_run, decision_gate_meta = apply_decision_gate_fn(
            session=session,
            run=run,
            tenant=tenant,
            settings=settings,
            send_discord_message_fn=send_discord_message_fn,
            send_jira_message_fn=send_jira_message_fn,
            ask_reply_components_fn=ask_reply_components_fn,
            blocked_status=run_status_blocked,
            failed_status=run_status_failed,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "worker_decision_gate_failed run_id=%s tenant_id=%s issue_key=%s error=%s",
            run.run_id,
            run.tenant_id,
            run.issue_key,
            exc,
        )
        return fail_guardrail_violation_fn(
            session,
            run=run,
            error=f"Decision Gate evaluation failed: {exc}",
        )
    if decision_gate_run is not None:
        if decision_gate_meta and isinstance(decision_gate_meta.get("send_result"), object):
            send_result = decision_gate_meta["send_result"]
            stage_update = decision_gate_meta["stage_update"]
            if not send_result.sent:
                logger.info(
                    "worker_discord_stage_update_not_sent tenant_id=%s run_id=%s stage=%s reason=%s",
                    run.tenant_id,
                    run.run_id,
                    stage_update["stage"],
                    send_result.reason,
                )
        return decision_gate_run

    project = resolve_project_for_run_fn(session, run=run)
    if project is None:
        return fail_missing_project_mapping_fn(session, run=run)
    if project.is_archived:
        return block_archived_project_fn(session, run=run, project=project)
    try:
        ensure_project_repository_checkout_fn(session=session, tenant=tenant, project=project)
    except Exception as exc:  # noqa: BLE001
        return fail_project_repository_checkout_fn(session, run=run, error=str(exc))

    notifier = RunStageNotifier(
        session=session,
        tenant=tenant,
        run=run,
        settings=settings,
        project=project,
        send_discord_message=send_discord_message_fn,
        send_jira_message=send_jira_message_fn,
    )
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config,
        project_overrides=project.policy_overrides,
    )
    _emit_queue_wait_metric(
        session=session,
        run=run,
        project_id=project.project_id,
        agent_id=agent_id,
    )
    if bool(effective_policy.get("allow_jira_transitions")):
        transition_issue_status_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            target_status="In Progress",
            settings=settings,
        )
    emit_agent_event_fn(
        event_type="TASK_STARTED",
        tenant_id=run.tenant_id,
        project_id=project.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        agent_id=agent_id,
    )

    bind_run_project_fn(session, run=run, project=project)

    try:
        workflow_request = workflow_request_for_run_fn(
            session,
            tenant,
            run,
            project=project,
            effective_policy=effective_policy,
        )
    except (PermissionError, ValueError) as exc:
        _cleanup_run_workspaces_safe(
            cleanup_run_workspaces_fn=cleanup_run_workspaces_fn,
            logger=logger,
            base_dir=settings.project_repo_checkout_base_dir,
            tenant_id=run.tenant_id,
            project_id=project.project_id,
            run_id=run.run_id,
            workspace_key=worker_workspace_key,
        )
        return fail_guardrail_violation_fn(session, run=run, error=str(exc))

    jira_issue_url = tenant_jira_issue_url_fn(session=session, tenant=tenant, issue_key=run.issue_key)
    run_dashboard_url = admin_run_url(admin_ui_base_url=settings.admin_ui_base_url, run_id=run.run_id)
    notifier.append(
        lock_acquired_update_fn(
            tenant_id=run.tenant_id,
            issue_key=run.issue_key,
            run_id=run.run_id,
            jira_url=jira_issue_url,
            run_url=run_dashboard_url,
        )
    )
    emit_agent_event_fn(
        event_type="LOCK_ACQUIRED",
        tenant_id=run.tenant_id,
        project_id=project.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        agent_id=agent_id,
    )

    def _emit_test_feedback(attempt: int, feedback: str) -> None:
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage="test_feedback",
            message=(
                f"Test feedback (attempt {attempt}) for run {run.run_id}:\n{feedback}\n"
                "Routing back to Dev for another iteration."
            ),
            settings=settings,
        )

    heartbeat_controller = build_run_heartbeat_controller_fn(
        run_id=run.run_id,
        worker_service_instance_id=worker_service_instance_id,
        heartbeat_interval_seconds=max(5, int(getattr(settings, "worker_run_heartbeat_interval_seconds", 30))),
    )
    execution_context = _execution_context(workflow_request=workflow_request)
    heartbeat_controller.start()
    try:
        workflow_result = runner.run(
            workflow_request,
            test_feedback_hook=_emit_test_feedback,
            stage_checkpoint_hook=lambda checkpoint: persist_stage_checkpoint_fn(
                session,
                run=run,
                checkpoint=checkpoint,
                execution_context=execution_context,
                expected_worker_service_instance_id=worker_service_instance_id,
            ),
        )
        session.refresh(run)
        if (
            str(run.worker_service_instance_id or "").strip() != str(worker_service_instance_id or "").strip()
            or run.status not in {run_status_running, run_status_cancelled}
        ):
            logger.warning(
                "worker_run_ownership_lost run_id=%s tenant_id=%s issue_key=%s status=%s current_owner=%s expected_owner=%s",
                run.run_id,
                run.tenant_id,
                run.issue_key,
                run.status,
                run.worker_service_instance_id,
                worker_service_instance_id,
            )
            return run
        if run.status == run_status_cancelled:
            _cleanup_run_workspaces_safe(
                cleanup_run_workspaces_fn=cleanup_run_workspaces_fn,
                logger=logger,
                base_dir=settings.project_repo_checkout_base_dir,
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
            )
            return finalize_cancelled_run_fn(
                session,
                run=run,
                stage_updates=notifier.stage_updates,
                expected_worker_service_instance_id=worker_service_instance_id,
            )
        if workflow_result.plan is not None:
            notifier.append(
                plan_posted_update_fn(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    jira_url=jira_issue_url,
                    run_url=run_dashboard_url,
                )
            )
            emit_agent_event_fn(
                event_type="PLAN_POSTED",
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                agent_id=agent_id,
            )
        if workflow_result.outcome == "success" and workflow_request.start_point_ref and workflow_request.start_point_sha:
            freshness = check_run_snapshot_freshness_fn(
                base_dir=settings.project_repo_checkout_base_dir,
                tenant_id=tenant.tenant_id,
                project=project,
                start_point_ref=workflow_request.start_point_ref,
                start_point_sha=workflow_request.start_point_sha,
            )
            if freshness.stale:
                error_text = freshness.message or "Branch snapshot stale; requeueing from latest snapshot."
                notifier.append(
                    run_requeued_stale_snapshot_update_fn(
                        tenant_id=run.tenant_id,
                        issue_key=run.issue_key,
                        run_id=run.run_id,
                        jira_url=jira_issue_url,
                        run_url=run_dashboard_url,
                        error=error_text,
                    )
                )
                _cleanup_run_workspaces_safe(
                    cleanup_run_workspaces_fn=cleanup_run_workspaces_fn,
                    logger=logger,
                    base_dir=settings.project_repo_checkout_base_dir,
                    tenant_id=run.tenant_id,
                    project_id=project.project_id,
                    run_id=run.run_id,
                )
                return requeue_workflow_result_for_stale_snapshot_fn(
                    session,
                    run=run,
                    workflow_result=workflow_result,
                    stage_updates=notifier.stage_updates,
                    error=error_text,
                    execution_context=execution_context,
                    expected_worker_service_instance_id=worker_service_instance_id,
                )
        if workflow_result.pr_url:
            notifier.append(
                pr_opened_update_fn(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    jira_url=jira_issue_url,
                    run_url=run_dashboard_url,
                    pr_url=workflow_result.pr_url,
                )
            )
            emit_agent_event_fn(
                event_type="PR_OPENED",
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                agent_id=agent_id,
            )
        if workflow_result.outcome == "requeue":
            capability_requeue_target = normalize_worker_capability(workflow_result.requeue_target)
            if capability_requeue_target is not None:
                required_worker_label = worker_label_for_capability(capability_requeue_target)
                error_text = workflow_result.requeue_reason or "Execution capability mismatch"
                notifier.append(
                    run_requeued_capability_update_fn(
                        tenant_id=run.tenant_id,
                        issue_key=run.issue_key,
                        run_id=run.run_id,
                        jira_url=jira_issue_url,
                        run_url=run_dashboard_url,
                        required_worker_label=required_worker_label,
                        error=error_text,
                    )
                )
                _cleanup_run_workspaces_safe(
                    cleanup_run_workspaces_fn=cleanup_run_workspaces_fn,
                    logger=logger,
                    base_dir=settings.project_repo_checkout_base_dir,
                    tenant_id=run.tenant_id,
                    project_id=project.project_id,
                    run_id=run.run_id,
                )
                return requeue_workflow_result_for_capability_fn(
                    session,
                    run=run,
                    workflow_result=workflow_result,
                    stage_updates=notifier.stage_updates,
                    required_worker_capability=capability_requeue_target,
                    required_worker_label=required_worker_label,
                    execution_context=execution_context,
                    expected_worker_service_instance_id=worker_service_instance_id,
                )

        if workflow_result.outcome == "waiting_for_input":
            session.refresh(run)
            if str(getattr(run, "status", "") or "").strip().lower() == RUN_STATUS_WAITING_FOR_INPUT:
                return run
        if workflow_result.outcome in {"blocked", "failed"}:
            error_text = (
                workflow_result.blocker_message
                or (workflow_result.diagnostics.message if workflow_result.diagnostics is not None else None)
                or "Workflow did not complete successfully"
            )
            notifier.append(
                run_failed_update_fn(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    jira_url=jira_issue_url,
                    run_url=run_dashboard_url,
                    error=error_text,
                )
            )
        finalization = WorkflowFinalizer(
            session=session,
            logger=logger,
            finalize_workflow_result_fn=finalize_workflow_result_fn,
            run_status_failed=run_status_failed,
            project_id=project.project_id,
            agent_id=agent_id,
        ).finalize(
            run=run,
            workflow_result=workflow_result,
            stage_updates=notifier.stage_updates,
            execution_context=execution_context,
            expected_worker_service_instance_id=worker_service_instance_id,
        )

        for event_type in finalization.event_types:
            emit_agent_event_fn(
                event_type=event_type,
                tenant_id=finalization.run.tenant_id,
                project_id=project.project_id,
                run_id=finalization.run.run_id,
                issue_key=finalization.run.issue_key,
                agent_id=agent_id,
            )

        CompletionTailExecutor(
            session=session,
            tenant=tenant,
            project=project,
            settings=settings,
            logger=logger,
            send_jira_message_fn=send_jira_message_fn,
            cleanup_run_workspaces_fn=cleanup_run_workspaces_fn,
            base_dir=settings.project_repo_checkout_base_dir,
            jira_issue_url=jira_issue_url,
            agent_id=agent_id,
            workspace_key=worker_workspace_key,
        ).execute(finalization)
        return finalization.run
    finally:
        heartbeat_controller.stop()

def _execution_context(*, workflow_request) -> dict[str, str] | None:  # noqa: ANN001
    context: dict[str, str] = {}
    for source_attr, field_name in (
        ("execution_repo_dir", "execution_repo_dir"),
        ("workspace_key", "workspace_key"),
        ("execution_branch", "execution_branch"),
        ("integration_branch", "integration_branch"),
        ("base_branch", "base_branch"),
        ("start_point_ref", "start_point_ref"),
        ("start_point_sha", "start_point_sha"),
    ):
        value = str(getattr(workflow_request, source_attr, "") or "").strip()
        if value:
            context[field_name] = value
    return context or None


def _cleanup_run_workspaces_safe(
    *,
    cleanup_run_workspaces_fn,
    logger,
    base_dir: str,
    tenant_id: str,
    project_id: str,
    run_id: str,
    workspace_key: str | None = None,
) -> None:  # noqa: ANN001
    try:
        cleanup_run_workspaces_fn(
            base_dir=base_dir,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            workspace_key=workspace_key,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "worker_run_workspace_cleanup_failed tenant_id=%s project_id=%s run_id=%s workspace_key=%s",
            tenant_id,
            project_id,
            run_id,
            workspace_key,
        )
