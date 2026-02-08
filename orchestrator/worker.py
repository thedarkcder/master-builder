from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import evaluate_decision_gate
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.core.enforcement_context import build_agent_enforcement_context
from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.logging import configure_logging
from orchestrator.core.signal_templates import (
    format_stage_discord_update,
    format_stage_jira_update,
)
from orchestrator.core.workflow_runner import WorkflowRequest, WorkflowRunner
from orchestrator.storage.models import Run, Tenant

logger = logging.getLogger(__name__)

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_CANCELLED = "cancelled"


def _coerce_positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _running_run_count(session: Session, *, tenant_id: str) -> int:
    return int(
        session.execute(
            select(func.count(Run.run_id)).where(
                Run.tenant_id == tenant_id,
                Run.status == RUN_STATUS_RUNNING,
            )
        ).scalar_one()
    )


def _workflow_request_for_run(tenant: Tenant, run: Run) -> WorkflowRequest:
    max_loops = _coerce_positive_int(
        tenant.policy_config.get("max_dev_test_review_loops"),
        default=1,
    )
    max_runtime_minutes = _coerce_positive_int(
        tenant.policy_config.get("max_runtime_minutes"),
        default=30,
    )
    suggested_test_commands_raw = tenant.policy_config.get("allowed_commands") or []
    suggested_test_commands: list[str] = []
    for command in suggested_test_commands_raw:
        command_text = str(command).strip()
        enforce_safe_command(command_text)
        suggested_test_commands.append(command_text)

    repo_root = Path(__file__).resolve().parents[1]
    enforcement_context = build_agent_enforcement_context(repo_root=repo_root)

    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary or f"Execute {run.issue_key}",
        issue_description=(
            f"{run.issue_description}\n\n{enforcement_context}"
            if (run.issue_description or "").strip()
            else enforcement_context
        ),
        max_dev_test_review_loops=max_loops,
        max_runtime_minutes=max_runtime_minutes,
        suggested_test_commands=suggested_test_commands,
    )


def process_next_queued_run(session: Session, runner: WorkflowRunner) -> Run | None:
    settings = get_settings()
    queued_runs = session.execute(
        select(Run).where(Run.status == RUN_STATUS_QUEUED).order_by(Run.created_at.asc())
    ).scalars().all()
    run: Run | None = None
    tenant: Tenant | None = None

    for candidate in queued_runs:
        candidate_tenant = session.get(Tenant, candidate.tenant_id)
        if candidate_tenant is None:
            candidate.status = RUN_STATUS_FAILED
            candidate.last_error = "Tenant not found for queued run"
            candidate.finished_at = datetime.now(timezone.utc)
            session.commit()
            session.refresh(candidate)
            return candidate

        max_concurrent_runs = _coerce_positive_int(
            candidate_tenant.policy_config.get("max_concurrent_runs"),
            default=1,
        )
        running_count = _running_run_count(session, tenant_id=candidate_tenant.tenant_id)
        if running_count >= max_concurrent_runs:
            logger.info(
                "worker_skipping_run_due_to_concurrency_limit tenant_id=%s issue_key=%s running=%s max=%s",
                candidate_tenant.tenant_id,
                candidate.issue_key,
                running_count,
                max_concurrent_runs,
            )
            continue

        run = candidate
        tenant = candidate_tenant
        break

    if run is None:
        return None

    try:
        decision_gate = evaluate_decision_gate(
            issue_summary=run.issue_summary,
            issue_description=run.issue_description,
        )
    except (FileNotFoundError, ValueError) as exc:
        run.status = RUN_STATUS_FAILED
        run.last_error = f"Decision Gate configuration error: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        return run
    if decision_gate.triggered:
        run.status = RUN_STATUS_BLOCKED
        run.last_error = f"Decision Gate required: {decision_gate.reason}"
        run.plan = {
            "succeeded": False,
            "attempts": 0,
            "summary": [],
            "test_guidance": [],
            "pr_url": None,
            "decision_gate": decision_gate.to_payload(),
        }
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        return run

    run.status = RUN_STATUS_RUNNING
    run.started_at = datetime.now(timezone.utc)
    session.commit()

    if tenant is None:
        raise RuntimeError("Tenant resolution failed for queued run")

    try:
        workflow_request = _workflow_request_for_run(tenant, run)
    except (PermissionError, ValueError) as exc:
        run.status = RUN_STATUS_FAILED
        run.last_error = f"Guardrail policy violation: {exc}"
        run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        return run

    stage_updates: list[dict[str, str]] = []
    jira_issue_url = (
        f"https://example.atlassian.net/browse/{run.issue_key}"
        if run.issue_key
        else None
    )

    def append_stage_update(stage_update: dict[str, str]) -> None:
        stage_updates.append(stage_update)
        send_result = send_tenant_discord_message(
            session=session,
            tenant=tenant,
            message=stage_update["discord_message"],
            settings=settings,
        )
        if not send_result.sent:
            logger.info(
                "worker_discord_stage_update_not_sent tenant_id=%s run_id=%s stage=%s reason=%s",
                run.tenant_id,
                run.run_id,
                stage_update["stage"],
                send_result.reason,
            )

    append_stage_update(
        {
            "stage": "lock_acquired",
            "tenant_id": run.tenant_id,
            "issue_key": run.issue_key,
            "run_id": run.run_id,
            "jira_message": format_stage_jira_update(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                stage="lock_acquired",
                jira_url=jira_issue_url,
            ),
            "discord_message": format_stage_discord_update(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                stage="lock_acquired",
                jira_url=jira_issue_url,
            ),
        }
    )

    workflow_result = runner.run(workflow_request)
    session.refresh(run)
    if run.status == RUN_STATUS_CANCELLED:
        run.plan = {
            "succeeded": False,
            "attempts": 0,
            "summary": ["Run cancelled during execution"],
            "test_guidance": [],
            "pr_url": run.pr_url,
            "stage_updates": stage_updates,
        }
        if run.finished_at is None:
            run.finished_at = datetime.now(timezone.utc)
        session.commit()
        session.refresh(run)
        return run
    plan_payload = workflow_result.to_plan_payload()
    if workflow_result.plan is not None:
        append_stage_update(
            {
                "stage": "plan_posted",
                "tenant_id": run.tenant_id,
                "issue_key": run.issue_key,
                "run_id": run.run_id,
                "jira_message": format_stage_jira_update(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    stage="plan_posted",
                    jira_url=jira_issue_url,
                ),
                "discord_message": format_stage_discord_update(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    stage="plan_posted",
                    jira_url=jira_issue_url,
                ),
            }
        )
    if workflow_result.pr_url:
        append_stage_update(
            {
                "stage": "pr_opened",
                "tenant_id": run.tenant_id,
                "issue_key": run.issue_key,
                "run_id": run.run_id,
                "jira_message": format_stage_jira_update(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    stage="pr_opened",
                    jira_url=jira_issue_url,
                    pr_url=workflow_result.pr_url,
                ),
                "discord_message": format_stage_discord_update(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    stage="pr_opened",
                    jira_url=jira_issue_url,
                    pr_url=workflow_result.pr_url,
                ),
            }
        )
    if not workflow_result.succeeded:
        error_text = (
            workflow_result.diagnostics.message
            if workflow_result.diagnostics is not None
            else "Workflow failed without diagnostics"
        )
        append_stage_update(
            {
                "stage": "run_failed",
                "tenant_id": run.tenant_id,
                "issue_key": run.issue_key,
                "run_id": run.run_id,
                "jira_message": format_stage_jira_update(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    stage="run_failed",
                    jira_url=jira_issue_url,
                    error=error_text,
                    next_steps=(
                        "Review diagnostics and follow-up issue payload.",
                        "Apply fix and move issue back to To Do when ready.",
                    ),
                ),
                "discord_message": format_stage_discord_update(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    stage="run_failed",
                    jira_url=jira_issue_url,
                    error=error_text,
                    next_steps=(
                        "Review diagnostics and follow-up issue payload.",
                        "Apply fix and move issue back to To Do when ready.",
                    ),
                ),
            }
        )

    plan_payload["stage_updates"] = stage_updates
    run.plan = plan_payload
    run.pr_url = workflow_result.pr_url
    run.finished_at = datetime.now(timezone.utc)
    if workflow_result.succeeded:
        run.status = RUN_STATUS_SUCCEEDED
        run.last_error = None
    else:
        run.status = RUN_STATUS_FAILED
        if workflow_result.diagnostics is not None:
            run.last_error = workflow_result.diagnostics.message
        else:
            run.last_error = "Workflow failed without diagnostics"

    session.commit()
    session.refresh(run)
    return run


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    logger.info("worker_started")
    await stop_event.wait()
    logger.info("worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
