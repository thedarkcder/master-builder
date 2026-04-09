from __future__ import annotations

import logging
from types import SimpleNamespace

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core import decision_engine as decision_engine_runtime
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.runs import mark_run_terminal
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
    evaluate_worker_decision_fn=decision_engine_runtime.evaluate_worker_decision,
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

    pre_check = getattr(worker_decision, "pre_check", None)
    block_reason = str(getattr(worker_decision, "block_reason", "") or "").strip()
    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip()
    if block_reason == "missing_ready_label":
        reason = (
            f"{enqueue_reason_guidance('missing_ready_label')} ({ready_label})"
            if ready_label
            else enqueue_reason_guidance("missing_ready_label")
        )
        next_steps = (
            [f"Apply ready label `{ready_label}` to the Jira issue, then retry the run."]
            if ready_label
            else ["Apply the configured ready label to the Jira issue, then retry the run."]
        )
    else:
        fallback_reason = enqueue_reason_guidance(block_reason or "decision_gate_required")
        decision_gate_reason = (
            str(getattr(getattr(worker_decision, "decision_gate", None), "reason", "") or "").strip()
        )
        reason = decision_gate_reason or fallback_reason
        next_steps = ["Reply with the required clarification on the issue, then retry the run."]

    jira_url = tenant_jira_issue_url(session=session, tenant=tenant, issue_key=run.issue_key)
    stage_update = run_not_ready_update(
        tenant_id=run.tenant_id,
        issue_key=run.issue_key,
        run_id=run.run_id,
        jira_url=jira_url,
        run_url=admin_run_url(admin_ui_base_url=settings.admin_ui_base_url, run_id=run.run_id),
        reason=reason,
        next_steps=next_steps,
    )
    session.refresh(run, attribute_names=["plan"])
    snapshot = ExecutionSnapshot.require(run.plan, allow_empty=True)
    snapshot.workflow = SnapshotWorkflow(
        outcome="blocked",
        attempts=0,
        summary=[],
        blocker_message=reason,
        requeue_target=None,
        requeue_reason=None,
    )
    snapshot.events.stage_updates = [stage_update]
    snapshot.context.execution_context["run_not_ready"] = {
        "ready_label": ready_label or None,
        "reason": reason,
        "block_reason": block_reason or None,
    }
    snapshot.context.execution_context["pre_check_outcome"] = (
        getattr(pre_check, "outcome", None) if pre_check is not None else None
    )
    run.plan = snapshot.dump()
    terminal_run = terminalizer(
        session=session,
        run_id=run.run_id,
        terminal_status=blocked_status,
        last_error=reason,
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
            event="decision_gate_required",
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
