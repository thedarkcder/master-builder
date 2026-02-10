from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import threading
from functools import lru_cache
from pathlib import Path

from sqlalchemy.orm import Session

from orchestrator.core.codex_agents import CodexWorkflowAgents
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import evaluate_decision_gate
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.core.enforcement_context import build_agent_enforcement_context
from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.jira_links import tenant_jira_issue_url
from orchestrator.core.logging import configure_logging
from orchestrator.core.worker_decision_gate import apply_decision_gate
from orchestrator.core.worker_jira_stage_service import send_stage_update_to_jira as _send_stage_update_to_jira
from orchestrator.core.worker_queue_selector import coerce_positive_int, select_next_queued_run
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
    max_loops = coerce_positive_int(
        effective_policy.get("max_dev_test_review_loops"),
        default=1,
    )
    max_runtime_minutes = coerce_positive_int(
        effective_policy.get("max_runtime_minutes"),
        default=30,
    )
    suggested_test_commands_raw = effective_policy.get("allowed_commands") or []
    suggested_test_commands: list[str] = []
    for command in suggested_test_commands_raw:
        command_text = str(command).strip()
        enforce_safe_command(command_text)
        suggested_test_commands.append(command_text)

    settings = get_settings()
    enforcement_context = _cached_enforcement_context(settings.required_codex_assets_version or "")

    issue_description = run.issue_description or ""
    if project is not None:
        project_context = (
            "\n\nProject routing context:\n"
            f"- project_id: {project.project_id}\n"
            f"- project_name: {project.name}\n"
            f"- github_repository: {project.github_repository}\n"
            f"- jira_project_key: {project.jira_project_key}\n"
        )
        issue_description = f"{issue_description}{project_context}".strip()
    else:
        issue_description = issue_description.strip()
    issue_description_with_enforcement = (
        f"{issue_description}\n\n{enforcement_context}" if issue_description else enforcement_context
    )

    return WorkflowRequest(
        tenant_id=tenant.tenant_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        issue_summary=run.issue_summary or f"Execute {run.issue_key}",
        issue_description=issue_description_with_enforcement,
        max_dev_test_review_loops=max_loops,
        max_runtime_minutes=max_runtime_minutes,
        suggested_test_commands=suggested_test_commands,
    )


@lru_cache(maxsize=1)
def _cached_enforcement_context(required_assets_version: str) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return build_agent_enforcement_context(
        repo_root=repo_root,
        required_assets_version=required_assets_version or None,
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


class _RunQueueNotificationBridge:
    def __init__(
        self,
        *,
        postgres_dsn: str,
        wake_event: asyncio.Event,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._postgres_dsn = postgres_dsn
        self._wake_event = wake_event
        self._loop = loop
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._conn = None
        self._conn_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="run-queue-listener",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._conn_lock:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        if psycopg is None:
            logger.error("worker_queue_listener_unavailable reason=missing_psycopg")
            self._loop.call_soon_threadsafe(self._wake_event.set)
            return
        try:
            with psycopg.connect(self._postgres_dsn, autocommit=True) as conn:
                with self._conn_lock:
                    self._conn = conn
                conn.execute(f'LISTEN "{RUN_QUEUE_NOTIFY_CHANNEL}"')
                # Wake once on startup to drain any queued runs that predate the listener.
                self._loop.call_soon_threadsafe(self._wake_event.set)
                for _notification in conn.notifies():
                    if self._stop_event.is_set():
                        break
                    self._loop.call_soon_threadsafe(self._wake_event.set)
        except Exception:
            logger.exception("worker_queue_listener_failed")
            self._loop.call_soon_threadsafe(self._wake_event.set)
        finally:
            with self._conn_lock:
                self._conn = None


async def _wait_for_wake_or_stop(
    *,
    wake_event: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    if wake_event.is_set() or stop_event.is_set():
        return
    wake_task = asyncio.create_task(wake_event.wait())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {wake_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        with contextlib.suppress(asyncio.CancelledError):
            task.result()


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
    listener = _RunQueueNotificationBridge(
        postgres_dsn=postgres_dsn_from_database_url(settings.database_url),
        wake_event=wake_event,
        loop=loop,
    )
    listener.start()

    logger.info("worker_started")
    try:
        while not stop_event.is_set():
            await _wait_for_wake_or_stop(wake_event=wake_event, stop_event=stop_event)
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
