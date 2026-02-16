from __future__ import annotations

from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.worker_capabilities import parse_worker_capabilities
from orchestrator.core.worker.stage_notifier import RunStageNotifier


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
    ensure_project_repository_checkout_fn,
    fail_project_repository_checkout_fn,
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
    transition_issue_status_fn,
    emit_agent_event_fn,
    resolve_agent_id_fn,
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
        worker_capabilities=parse_worker_capabilities(getattr(settings, "worker_capabilities", "")),
    )
    if selection.terminal_run is not None:
        return selection.terminal_run
    if selection.run is None or selection.tenant is None:
        return None
    run = selection.run
    tenant = selection.tenant
    agent_id = resolve_agent_id_fn()

    emit_agent_event_fn(
        event_type="ISSUE_ASSIGNED",
        tenant_id=run.tenant_id,
        project_id=run.project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        agent_id=agent_id,
    )

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
    start_run_fn(session, run=run)
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
            tenant,
            run,
            project=project,
            effective_policy=effective_policy,
        )
    except (PermissionError, ValueError) as exc:
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

    workflow_result = runner.run(workflow_request, test_feedback_hook=_emit_test_feedback)
    _emit_detailed_jira_feedback(
        session=session,
        tenant=tenant,
        run=run,
        settings=settings,
        workflow_result=workflow_result,
        send_jira_message_fn=send_jira_message_fn,
    )
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
                run_url=run_dashboard_url,
                error=error_text,
            )
        )
        emit_agent_event_fn(
            event_type="RUN_FAILED",
            tenant_id=run.tenant_id,
            project_id=project.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
        )
        emit_agent_event_fn(
            event_type="TASK_FAILED",
            tenant_id=run.tenant_id,
            project_id=project.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
        )
        diagnostics_stage = (
            workflow_result.diagnostics.stage.upper()
            if workflow_result.diagnostics is not None and workflow_result.diagnostics.stage
            else ""
        )
        if diagnostics_stage == "BUILD":
            emit_agent_event_fn(
                event_type="BUILD_FAILED",
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                agent_id=agent_id,
            )
        if diagnostics_stage == "TEST":
            emit_agent_event_fn(
                event_type="TEST_FAILED",
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                agent_id=agent_id,
            )
    else:
        emit_agent_event_fn(
            event_type="TASK_COMPLETED",
            tenant_id=run.tenant_id,
            project_id=project.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
        )

    return finalize_workflow_result_fn(
        session,
        run=run,
        workflow_result=workflow_result,
        stage_updates=notifier.stage_updates,
    )
def _format_multiline_jira_comment(*, title: str, lines: list[str], run_id: str) -> str:
    content_lines = [str(line) for line in lines if str(line).strip()]
    if not content_lines:
        return ""
    rendered = [f"{title} (run {run_id}):"]
    rendered.extend(f"- {line}" for line in content_lines)
    return "\n".join(rendered)


def _emit_detailed_jira_feedback(
    *,
    session,
    tenant,
    run,
    settings,
    workflow_result,
    send_jira_message_fn,
):  # noqa: ANN001
    dev_comment = _format_multiline_jira_comment(
        title="Dev rationale",
        lines=list(workflow_result.dev_rationale or []),
        run_id=run.run_id,
    )
    if dev_comment:
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage="dev_rationale",
            message=dev_comment,
            settings=settings,
        )

    review_lines = list(workflow_result.review_summary or [])
    review_comment = _format_multiline_jira_comment(
        title="Review summary",
        lines=review_lines,
        run_id=run.run_id,
    )
    if review_comment:
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage="review_summary",
            message=review_comment,
            settings=settings,
        )

    review_feedback = str(workflow_result.review_feedback or "").strip()
    if review_feedback:
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage="review_feedback",
            message=f"Review feedback (run {run.run_id}):\n{review_feedback}",
            settings=settings,
        )
