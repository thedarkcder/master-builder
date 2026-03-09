from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import delete

from orchestrator.core.decision_engine import DecisionEventInput, evaluate_decision_event
from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.runs import mark_run_terminal
from orchestrator.storage.models import RunLock
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run
from orchestrator.core.worker.stage_events import decision_gate_required_update


def apply_decision_gate(
    *,
    session,
    run,
    tenant,
    settings,
    tenant_jira_oauth_context_fn,
    evaluate_pre_run_check_fn=evaluate_pre_run_check,
    send_discord_message_fn,
    send_jira_message_fn,
    ask_reply_components_fn,
    blocked_status: str,
    failed_status: str,
) -> tuple[object | None, dict | None]:
    result = evaluate_decision_event(
        session=session,
        tenant=tenant,
        project=None,
        event=DecisionEventInput(
            source="worker_execution",
            event_type="worker_execution_gate",
            idempotency_key=f"worker-execution:{run.run_id}",
            issue_key=run.issue_key,
            issue_summary=run.issue_summary,
            issue_description=run.issue_description,
            issue_labels=[],
        ),
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        publish_jira_comment_fn=None,
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )

    if result.decision.policy_error:
        run.status = failed_status
        run.last_error = f"Decision Gate configuration error: {result.decision.policy_error}"
        run.finished_at = datetime.now(timezone.utc)
        session.execute(
            delete(RunLock).where(
                RunLock.tenant_id == run.tenant_id,
                RunLock.issue_key == run.issue_key,
                RunLock.run_id == run.run_id,
            )
        )
        session.commit()
        session.refresh(run)
        return run, None

    if result.decision.block_reason not in {"decision_gate_required", "gtd_required"}:
        return None, None

    pre_check = result.decision.pre_check
    decision_gate = getattr(pre_check, "decision_gate", None)
    gtd = getattr(pre_check, "gtd", None)
    reason = ""
    questions: list[str] = []
    if result.decision.block_reason == "decision_gate_required" and decision_gate is not None:
        reason = str(getattr(decision_gate, "reason", "") or "").strip()
        questions = [str(question).strip() for question in getattr(decision_gate, "questions", ()) if str(question).strip()]
        decision_gate_payload = decision_gate.to_payload()
    else:
        missing = [
            str(item).strip()
            for item in getattr(gtd, "missing_criteria", ())
            if str(item).strip()
        ]
        reason = "Good To Do details are incomplete."
        if missing:
            reason = f"Missing GTD criteria: {', '.join(missing)}"
        questions = [str(question).strip() for question in getattr(gtd, "clarification_questions", ()) if str(question).strip()]
        decision_gate_payload = {
            "triggered": True,
            "reason": reason,
            "missing_sections": missing,
            "questions": questions,
            "recommendation": "Clarification required before execution.",
            "tags": [],
        }

    jira_url = tenant_jira_issue_url(session=session, tenant=tenant, issue_key=run.issue_key)
    stage_update = decision_gate_required_update(
        tenant_id=run.tenant_id,
        issue_key=run.issue_key,
        run_id=run.run_id,
        jira_url=jira_url,
        run_url=admin_run_url(admin_ui_base_url=settings.admin_ui_base_url, run_id=run.run_id),
        reason=reason,
        questions=questions,
    )
    run.plan = {
        "succeeded": False,
        "attempts": 0,
        "summary": [],
        "test_guidance": [],
        "pr_url": None,
        "stage_updates": [stage_update],
        "decision_gate": decision_gate_payload,
        "pre_check": {
            "outcome": getattr(pre_check, "outcome", None),
        },
    }
    terminal_run = mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=blocked_status,
        last_error=f"Decision Gate required: {reason}",
    )
    project = resolve_project_for_run(session, run=run)
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
            open_thread=True,
            thread_name=f"{run.issue_key}-decision-gate",
            thread_intro=(
                "Reply here with clarification questions, then update the Jira issue with GTD details "
                "and run !retry <ISSUE_KEY>."
            ),
            thread_intro_components=ask_reply_components_fn(),
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
