from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy.orm import Session

from orchestrator.core.codex_agents import CodexWorkflowAgents
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import evaluate_decision_gate
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.logging import configure_logging
from orchestrator.core.worker_decision_gate import apply_decision_gate
from orchestrator.core.worker_jira_stage_service import send_stage_update_to_jira as _send_stage_update_to_jira
from orchestrator.core.worker_queue_listener import (
    RunQueueNotificationBridge,
    wait_for_wake_or_stop,
)
from orchestrator.core.worker_queue_selector import select_next_queued_run
from orchestrator.core.worker_run_lifecycle import (
    bind_run_project,
    block_archived_project,
    fail_guardrail_violation,
    fail_missing_project_mapping,
    finalize_cancelled_run,
    finalize_workflow_result,
    resolve_project_for_run,
    start_run,
)
from orchestrator.core.worker_process_service import process_next_queued_run as _process_next_queued_run_impl
from orchestrator.core.worker_stage_events import (
    lock_acquired_update,
    plan_posted_update,
    pr_opened_update,
    run_failed_update,
)
from orchestrator.core.worker_workflow_request_service import (
    build_workflow_request_for_run as _build_workflow_request_for_run,
)
from orchestrator.core.workflow_runner import WorkflowRequest, WorkflowRunner
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.storage.run_queue_events import (
    RUN_QUEUE_NOTIFY_CHANNEL,
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None

logger = logging.getLogger(__name__)

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_SUCCEEDED = "succeeded"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_BLOCKED = "blocked"
RUN_STATUS_CANCELLED = "cancelled"
ASK_REPLY_OPEN_CUSTOM_ID = "ask.reply.open"


def _ask_reply_components() -> list[dict]:
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 2,
                    "label": "Reply",
                    "custom_id": ASK_REPLY_OPEN_CUSTOM_ID,
                }
            ],
        }
    ]


def _workflow_request_for_run(
    tenant: Tenant,
    run: Run,
    *,
    project: Project | None,
    effective_policy: dict,
) -> WorkflowRequest:
    return _build_workflow_request_for_run(
        tenant=tenant,
        run=run,
        project=project,
        effective_policy=effective_policy,
        settings=get_settings(),
    )


def process_next_queued_run(session: Session, runner: WorkflowRunner) -> Run | None:
    return _process_next_queued_run_impl(
        session=session,
        runner=runner,
        logger=logger,
        settings_fn=get_settings,
        select_next_queued_run_fn=select_next_queued_run,
        apply_decision_gate_fn=lambda **kwargs: apply_decision_gate(
            evaluate_decision_gate_fn=evaluate_decision_gate,
            **kwargs,
        ),
        send_discord_message_fn=send_tenant_discord_message,
        send_jira_message_fn=_send_stage_update_to_jira,
        ask_reply_components_fn=_ask_reply_components,
        resolve_project_for_run_fn=resolve_project_for_run,
        fail_missing_project_mapping_fn=fail_missing_project_mapping,
        block_archived_project_fn=block_archived_project,
        start_run_fn=start_run,
        bind_run_project_fn=bind_run_project,
        workflow_request_for_run_fn=_workflow_request_for_run,
        fail_guardrail_violation_fn=fail_guardrail_violation,
        tenant_jira_issue_url_fn=tenant_jira_issue_url,
        lock_acquired_update_fn=lock_acquired_update,
        plan_posted_update_fn=plan_posted_update,
        pr_opened_update_fn=pr_opened_update,
        run_failed_update_fn=run_failed_update,
        finalize_cancelled_run_fn=finalize_cancelled_run,
        finalize_workflow_result_fn=finalize_workflow_result,
        run_status_queued=RUN_STATUS_QUEUED,
        run_status_running=RUN_STATUS_RUNNING,
        run_status_failed=RUN_STATUS_FAILED,
        run_status_blocked=RUN_STATUS_BLOCKED,
        run_status_cancelled=RUN_STATUS_CANCELLED,
    )


def _build_workflow_runner_for_session(*, session: Session) -> WorkflowRunner:
    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    agents = CodexWorkflowAgents(runtime=runtime)
    return WorkflowRunner(agents)


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    session_factory = create_session_factory()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    if not is_postgres_database_url(settings.database_url):
        raise RuntimeError(
            "Event-driven worker requires PostgreSQL (LISTEN/NOTIFY); "
            "set ORCHESTRATOR_DATABASE_URL to a postgresql URL."
        )

    wake_event = asyncio.Event()
    listener = RunQueueNotificationBridge(
        postgres_dsn=postgres_dsn_from_database_url(settings.database_url),
        wake_event=wake_event,
        loop=loop,
        logger=logger,
        notify_channel=RUN_QUEUE_NOTIFY_CHANNEL,
        psycopg_module=psycopg,
    )
    listener.start()

    logger.info("worker_started")
    try:
        while not stop_event.is_set():
            await wait_for_wake_or_stop(wake_event=wake_event, stop_event=stop_event)
            if stop_event.is_set():
                break
            wake_event.clear()

            while not stop_event.is_set():
                with session_factory() as session:
                    try:
                        runner = _build_workflow_runner_for_session(session=session)
                    except CodexRuntimeError as exc:
                        raise RuntimeError(f"Worker runtime unavailable: {exc}") from exc
                    processed = process_next_queued_run(session, runner)
                if processed is None:
                    break
    finally:
        listener.stop()
        logger.info("worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
