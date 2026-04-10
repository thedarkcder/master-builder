from __future__ import annotations

import logging
from types import SimpleNamespace
from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core.decision_types import WorkerStageEvent
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.worker.run_not_ready import derive_run_not_ready_outcome
from orchestrator.core.worker.readiness import evaluate_worker_decision as evaluate_worker_readiness_decision
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run
from orchestrator.core.worker.stage_events import run_not_ready_update
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.execution_snapshot import SnapshotWorkflow

logger = logging.getLogger(__name__)


def _load_live_issue_context(
    *,
    session,
    tenant,
    settings,
    tenant_jira_oauth_context_fn,
    issue_key: str,
    fallback_summary: str | None,
    fallback_description: str | None,
) -> tuple[str | None, str | None, list[str] | None]:
    issue_summary = fallback_summary
    issue_description = fallback_description
    issue_labels: list[str] | None = None
    try:
        oauth = tenant_jira_oauth_context_fn(session=session, tenant=tenant, settings=settings)
        client = getattr(oauth, "client", None)
        connection = getattr(oauth, "connection", None)
        access_token = getattr(oauth, "access_token", None)
        cloud_id = getattr(connection, "cloud_id", None)
        if client is None or access_token is None or not str(cloud_id or "").strip():
            return issue_summary, issue_description, issue_labels
        issue_detail = client.get_issue_detail(
            access_token=access_token,
            cloud_id=str(cloud_id),
            issue_id_or_key=issue_key,
        )
        issue_summary = str(getattr(issue_detail, "summary", "") or "").strip() or issue_summary
        issue_description = str(getattr(issue_detail, "description", "") or "").strip() or issue_description
        labels_raw = getattr(issue_detail, "labels", None)
        if isinstance(labels_raw, (list, tuple, set)):
            issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "worker_issue_context_refresh_failed tenant_id=%s issue_key=%s error=%s",
            getattr(tenant, "tenant_id", None),
            issue_key,
            exc,
        )
    return issue_summary, issue_description, issue_labels


def apply_decision_gate(
    *,
    session,
    run,
    tenant,
    settings,
    tenant_jira_oauth_context_fn,
    evaluate_worker_decision_fn=evaluate_worker_readiness_decision,
    send_discord_message_fn,
    send_jira_message_fn,
    ask_reply_components_fn,
    blocked_status: str,
    failed_status: str,
    mark_run_terminal_fn=None,
) -> tuple[object | None, dict | None]:
    terminalizer = mark_run_terminal if mark_run_terminal_fn is None else mark_run_terminal_fn
    project = resolve_project_for_run(session, run=run)
    issue_summary, issue_description, issue_labels = _load_live_issue_context(
        session=session,
        tenant=tenant,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        issue_key=run.issue_key,
        fallback_summary=run.issue_summary,
        fallback_description=run.issue_description,
    )
    try:
        worker_decision = evaluate_worker_decision_fn(
            run_plan=getattr(run, "plan", None),
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            issue_key=run.issue_key,
            run_id=run.run_id,
            issue_summary=issue_summary,
            issue_description=issue_description,
            session=session,
            tenant=tenant,
            project=project,
            issue_labels=issue_labels,
            settings=settings,
            tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        )
    except Exception as exc:  # noqa: BLE001
        error_text = f"Execution readiness check failed: {exc}"
        terminal_run = terminalizer(
            session=session,
            run_id=run.run_id,
            terminal_status=failed_status,
            last_error=error_text,
        )
        return terminal_run, None

    if worker_decision.allowed:
        return None, None

    if worker_decision.decision_gate is None:
        error_text = (
            worker_decision.configuration_error
            or "Execution readiness check failed: worker decision rejected run without decision-gate details"
        )
        terminal_run = terminalizer(
            session=session,
            run_id=run.run_id,
            terminal_status=failed_status,
            last_error=error_text,
        )
        return terminal_run, None

    try:
        run_not_ready = derive_run_not_ready_outcome(worker_decision=worker_decision)
    except ValueError as exc:
        error_text = f"Execution readiness check failed: {exc}"
        terminal_run = terminalizer(
            session=session,
            run_id=run.run_id,
            terminal_status=failed_status,
            last_error=error_text,
        )
        return terminal_run, None

    jira_url = tenant_jira_issue_url(session=session, tenant=tenant, issue_key=run.issue_key)
    stage_update = run_not_ready_update(
        tenant_id=run.tenant_id,
        issue_key=run.issue_key,
        run_id=run.run_id,
        jira_url=jira_url,
        run_url=admin_run_url(admin_ui_base_url=settings.admin_ui_base_url, run_id=run.run_id),
        reason=run_not_ready.reason,
        next_steps=run_not_ready.next_steps,
    )
    session.refresh(run, attribute_names=["plan"])
    snapshot = ExecutionSnapshot.require(run.plan, allow_empty=True)
    snapshot.workflow = SnapshotWorkflow(
        outcome="blocked",
        attempts=0,
        summary=[],
        blocker_message=run_not_ready.reason,
        requeue_target=None,
        requeue_reason=None,
    )
    snapshot.events.stage_updates = [stage_update]
    snapshot.context.execution_context["run_not_ready"] = {
        "ready_label": run_not_ready.ready_label,
        "reason": run_not_ready.reason,
        "block_reason": run_not_ready.block_reason,
    }
    snapshot.context.execution_context["pre_check_outcome"] = run_not_ready.pre_check_outcome
    run.plan = snapshot.dump()
    terminal_run = terminalizer(
        session=session,
        run_id=run.run_id,
        terminal_status=blocked_status,
        last_error=run_not_ready.reason,
    )
    send_result = SimpleNamespace(sent=False, reason="not_attempted")
    send_error: str | None = None
    try:
        send_result = send_discord_message_fn(
            session=session,
            tenant=tenant,
            project=project,
            message=stage_update["discord_message"],
            settings=settings,
            event=WorkerStageEvent.DECISION_GATE_REQUIRED.value,
            open_thread=False,
        )
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage=stage_update["stage"],
            message=stage_update["jira_message"],
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        send_error = str(exc)
    return terminal_run, {
        "stage_update": stage_update,
        "send_result": send_result,
        "send_error": send_error,
    }
