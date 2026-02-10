from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.worker_run_lifecycle import resolve_project_for_run
from orchestrator.core.worker_stage_events import decision_gate_required_update


def apply_decision_gate(
    *,
    session,
    run,
    tenant,
    settings,
    evaluate_decision_gate_fn,
    send_discord_message_fn,
    send_jira_message_fn,
    ask_reply_components_fn,
    blocked_status: str,
    failed_status: str,
) -> tuple[object | None, dict | None]:
    try:
        decision_gate = evaluate_decision_gate_fn(
            issue_summary=run.issue_summary,
            issue_description=run.issue_description,
        )
    except (FileNotFoundError, ValueError) as exc:
        run.status = failed_status
        run.last_error = f"Decision Gate configuration error: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        return run, None

    if not decision_gate.triggered:
        return None, None

    jira_url = f"https://master-builder.atlassian.net/browse/{run.issue_key}" if run.issue_key else None
    stage_update = decision_gate_required_update(
        tenant_id=run.tenant_id,
        issue_key=run.issue_key,
        run_id=run.run_id,
        jira_url=jira_url,
        reason=decision_gate.reason,
        questions=decision_gate.questions,
    )
    send_result = send_discord_message_fn(
        session=session,
        tenant=tenant,
        project=resolve_project_for_run(session, run=run),
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
    run.plan = {
        "succeeded": False,
        "attempts": 0,
        "summary": [],
        "test_guidance": [],
        "pr_url": None,
        "stage_updates": [stage_update],
        "decision_gate": decision_gate.to_payload(),
    }
    terminal_run = mark_run_terminal(
        session,
        run_id=run.run_id,
        terminal_status=blocked_status,
        last_error=f"Decision Gate required: {decision_gate.reason}",
    )
    return terminal_run, {
        "stage_update": stage_update,
        "send_result": send_result,
    }
