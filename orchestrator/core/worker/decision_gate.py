from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.pre_run_check import evaluate_execution_readiness_only
from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run
from orchestrator.core.worker.stage_events import run_not_ready_update
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.execution_snapshot import SnapshotWorkflow


def _oauth_context_value(oauth_context: object, field: str) -> object | None:
    if isinstance(oauth_context, dict):
        return oauth_context.get(field)
    return getattr(oauth_context, field, None)


def apply_decision_gate(
    *,
    session,
    run,
    tenant,
    settings,
    tenant_jira_oauth_context_fn,
    evaluate_execution_readiness_fn=evaluate_execution_readiness_only,
    send_discord_message_fn,
    send_jira_message_fn,
    ask_reply_components_fn,
    blocked_status: str,
    failed_status: str,
) -> tuple[object | None, dict | None]:
    project = resolve_project_for_run(session, run=run)
    try:
        oauth = tenant_jira_oauth_context_fn(session=session, tenant=tenant, settings=settings)
        oauth_client = _oauth_context_value(oauth, "client")
        oauth_connection = _oauth_context_value(oauth, "connection")
        oauth_access_token = _oauth_context_value(oauth, "access_token")
        cloud_id = getattr(oauth_connection, "cloud_id", None)
        if oauth_client is None or oauth_access_token is None or not str(cloud_id or "").strip():
            raise RuntimeError("Tenant Jira OAuth context is incomplete")
        issue_detail = oauth_client.get_issue_detail(
            access_token=str(oauth_access_token),
            cloud_id=str(cloud_id),
            issue_id_or_key=run.issue_key,
        )
        issue_summary = str(getattr(issue_detail, "summary", "") or "").strip() or run.issue_summary
        issue_description = str(getattr(issue_detail, "description", "") or "").strip() or run.issue_description
        raw_labels = getattr(issue_detail, "labels", None)
        issue_labels = [str(label).strip() for label in raw_labels if str(label).strip()] if isinstance(raw_labels, list) else []
        pre_check = evaluate_execution_readiness_fn(
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            issue_key=run.issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            ready_label=(tenant.jira_config or {}).get("ready_label"),
        )
    except Exception as exc:  # noqa: BLE001
        run.status = failed_status
        run.last_error = f"Execution readiness check failed: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        return run, None

    if str(pre_check.outcome or "").strip() != "missing_ready_label":
        return None, None

    ready_label = str(pre_check.ready_label or "").strip()
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
    }
    snapshot.context.execution_context["pre_check_outcome"] = (
        getattr(pre_check, "outcome", None) if pre_check is not None else None
    )
    run.plan = snapshot.dump()
    terminal_run = mark_run_terminal(
        session,
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
