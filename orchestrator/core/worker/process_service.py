from __future__ import annotations

import json
from uuid import uuid4

from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.run_logs import record_run_log_event
from orchestrator.core.worker_capabilities import (
    normalize_worker_capability,
    parse_worker_capabilities,
    worker_label_for_capability,
)
from orchestrator.core.worker.stage_notifier import RunStageNotifier


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
    run_requeued_capability_update_fn,
    finalize_cancelled_run_fn,
    finalize_workflow_result_fn,
    requeue_workflow_result_for_capability_fn,
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

    started_run = start_run_fn(
        session,
        run=run,
        expected_status=run_status_queued,
    )
    if started_run is None:
        logger.info(
            "worker_skipping_run_already_claimed run_id=%s tenant_id=%s issue_key=%s",
            run.run_id,
            run.tenant_id,
            run.issue_key,
        )
        return None
    run = started_run

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
    _emit_orchestrated_trace_logs(
        session=session,
        run=run,
        workflow_result=workflow_result,
        agent_id=agent_id,
    )
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
        capability_requeue_target = _extract_capability_requeue_target(workflow_result)
        if capability_requeue_target is not None:
            required_worker_label = worker_label_for_capability(capability_requeue_target)
            error_text = (
                workflow_result.diagnostics.message
                if workflow_result.diagnostics is not None
                else "Execution capability mismatch"
            )
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
            return requeue_workflow_result_for_capability_fn(
                session,
                run=run,
                workflow_result=workflow_result,
                stage_updates=notifier.stage_updates,
                required_worker_capability=capability_requeue_target,
                required_worker_label=required_worker_label,
            )
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


def _extract_capability_requeue_target(workflow_result) -> str | None:  # noqa: ANN001
    if workflow_result.succeeded:
        return None
    diagnostics = workflow_result.diagnostics
    if diagnostics is None or str(diagnostics.stage or "").strip().lower() != "pm":
        return None
    message = str(diagnostics.message or "")
    if "Execution capability mismatch:" not in message:
        return None
    plan = workflow_result.plan
    if plan is not None:
        plan_capability = normalize_worker_capability(plan.execution_worker_capability)
        if plan_capability is not None:
            return plan_capability
    for item in diagnostics.history or []:
        event = str(item.get("event") or "")
        if not event.startswith("execution_capability_mismatch:"):
            continue
        parts = event.split(":")[-1].split(",")
        for part in parts:
            key, _, raw_value = part.partition("=")
            if key.strip() != "required":
                continue
            parsed = normalize_worker_capability(raw_value.strip())
            if parsed is not None:
                return parsed
    return None


def _normalize_terminal_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"completed", "approved", "succeeded", "success"}:
        return "succeeded"
    if normalized in {"needs_changes", "failed", "failure"}:
        return "failed"
    if normalized == "blocked":
        return "blocked"
    return "succeeded"


def _emit_orchestrated_trace_logs(
    *,
    session,
    run,
    workflow_result,
    agent_id: str,
) -> None:  # noqa: ANN001
    stage_trace = list(getattr(workflow_result, "orchestration_stage_trace", []) or [])
    workstream_trace = list(getattr(workflow_result, "orchestration_workstream_trace", []) or [])
    if not stage_trace and not workstream_trace:
        return

    for index, item in enumerate(stage_trace):
        if not isinstance(item, dict):
            continue
        stage = str(item.get("stage") or "").strip().lower()
        if stage not in {"pm", "dev", "test", "review"}:
            continue
        attempt = int(item["attempt"]) if isinstance(item.get("attempt"), int) else 1
        invocation_id = str(item.get("invocation_id") or f"orchestrated-stage-{run.run_id}-{stage}-{index}").strip()
        status = str(item.get("status") or "").strip().lower()
        summary = str(item.get("summary") or "").strip()
        started_payload: dict[str, object] = {
            "event_kind": "stage_invocation_started",
            "status": "started",
            "source": "orchestrated_run_trace",
            "virtual_stage": stage,
            "trace_index": index,
        }
        if summary:
            started_payload["summary"] = summary
        record_run_log_event(
            session=session,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
            invocation_id=invocation_id,
            channel="worker",
            command=f"workflow.{stage}",
            working_dir=None,
            stage="telemetry",
            attempt=attempt,
            stream="system",
            message=json.dumps(started_payload, sort_keys=True),
        )
        finished_payload: dict[str, object] = {
            "event_kind": "stage_invocation_finished",
            "status": _normalize_terminal_status(status),
            "source": "orchestrated_run_trace",
            "virtual_stage": stage,
            "trace_index": index,
        }
        duration_ms = item.get("duration_ms")
        if isinstance(duration_ms, int):
            finished_payload["duration_ms"] = max(0, duration_ms)
        if summary:
            finished_payload["summary"] = summary
        record_run_log_event(
            session=session,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
            invocation_id=invocation_id,
            channel="worker",
            command=f"workflow.{stage}",
            working_dir=None,
            stage="telemetry",
            attempt=attempt,
            stream="system",
            message=json.dumps(finished_payload, sort_keys=True),
        )
        # Persist an explicit stage-scoped row so run logs can be filtered by PM/DEV/TEST/REVIEW
        # even when orchestration runs as a single top-level invocation.
        stage_row_payload = {
            "event_kind": "orchestrated_stage_event",
            "source": "orchestrated_run_trace",
            "trace_index": index,
            "stage": stage,
            "status": _normalize_terminal_status(status),
            "summary": summary,
        }
        record_run_log_event(
            session=session,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
            invocation_id=invocation_id,
            channel="worker",
            command=f"workflow.{stage}",
            working_dir=None,
            stage=stage,
            attempt=attempt,
            stream="system",
            message=json.dumps(stage_row_payload, sort_keys=True),
        )

    for index, item in enumerate(workstream_trace):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        stage = str(item.get("stage") or "dev").strip().lower() or "dev"
        if stage not in {"pm", "dev", "test", "review"}:
            stage = "dev"
        status = str(item.get("status") or "completed").strip().lower() or "completed"
        workstream_payload: dict[str, object] = {
            "event_kind": "orchestrated_workstream_event",
            "source": "orchestrated_run_trace",
            "trace_index": index,
            "name": name,
            "stage": stage,
            "status": status,
        }
        summary = str(item.get("summary") or "").strip()
        if summary:
            workstream_payload["summary"] = summary
        branch = str(item.get("branch") or "").strip()
        if branch:
            workstream_payload["branch"] = branch
        record_run_log_event(
            session=session,
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
            invocation_id=f"orchestrated-workstream-{run.run_id}-{index}",
            channel="worker",
            command=f"workflow.{stage}",
            working_dir=None,
            stage="telemetry",
            attempt=1,
            stream="system",
            message=json.dumps(workstream_payload, sort_keys=True),
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
