from __future__ import annotations

from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.worker_stage_notifier import RunStageNotifier


def process_next_queued_run(
    *,
    session,
    runner,
    logger,
    settings_fn,
    select_next_queued_run_fn,
    apply_decision_gate_fn,
    send_discord_message_fn,
    send_jira_message_fn,
    ask_reply_components_fn,
    resolve_project_for_run_fn,
    fail_missing_project_mapping_fn,
    block_archived_project_fn,
    start_run_fn,
    bind_run_project_fn,
    workflow_request_for_run_fn,
    fail_guardrail_violation_fn,
    tenant_jira_issue_url_fn,
    lock_acquired_update_fn,
    plan_posted_update_fn,
    pr_opened_update_fn,
    run_failed_update_fn,
    finalize_cancelled_run_fn,
    finalize_workflow_result_fn,
    run_status_queued: str,
    run_status_running: str,
    run_status_failed: str,
    run_status_blocked: str,
    run_status_cancelled: str,
):  # noqa: ANN001
    settings = settings_fn()
    selection = select_next_queued_run_fn(
        session,
        queued_status=run_status_queued,
        running_status=run_status_running,
        failed_status=run_status_failed,
    )
    if selection.terminal_run is not None:
        return selection.terminal_run
    if selection.run is None or selection.tenant is None:
        return None
    run = selection.run
    tenant = selection.tenant

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

    notifier = RunStageNotifier(
        session=session,
        tenant=tenant,
        run=run,
        settings=settings,
        project=project,
        send_discord_message=send_discord_message_fn,
        send_jira_message=send_jira_message_fn,
    )
    start_run_fn(session, run=run)

    bind_run_project_fn(session, run=run, project=project)
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config,
        project_overrides=project.policy_overrides,
    )

    try:
        workflow_request = workflow_request_for_run_fn(
            tenant,
            run,
            project=project,
            effective_policy=effective_policy,
        )
    except (PermissionError, ValueError) as exc:
        return fail_guardrail_violation_fn(session, run=run, error=str(exc))

    jira_issue_url = tenant_jira_issue_url_fn(session=session, tenant=tenant, issue_key=run.issue_key)
    notifier.append(
        lock_acquired_update_fn(
            tenant_id=run.tenant_id,
            issue_key=run.issue_key,
            run_id=run.run_id,
            jira_url=jira_issue_url,
        )
    )

    workflow_result = runner.run(workflow_request)
    session.refresh(run)
    if run.status == run_status_cancelled:
        return finalize_cancelled_run_fn(session, run=run, stage_updates=notifier.stage_updates)
    if workflow_result.plan is not None:
        notifier.append(
            plan_posted_update_fn(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                jira_url=jira_issue_url,
            )
        )
    if workflow_result.pr_url:
        notifier.append(
            pr_opened_update_fn(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                jira_url=jira_issue_url,
                pr_url=workflow_result.pr_url,
            )
        )
    if not workflow_result.succeeded:
        error_text = (
            workflow_result.diagnostics.message
            if workflow_result.diagnostics is not None
            else "Workflow failed without diagnostics"
        )
        notifier.append(
            run_failed_update_fn(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                jira_url=jira_issue_url,
                error=error_text,
            )
        )

    return finalize_workflow_result_fn(
        session,
        run=run,
        workflow_result=workflow_result,
        stage_updates=notifier.stage_updates,
    )
