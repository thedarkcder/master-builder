from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone
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
from orchestrator.core.logging import configure_logging
from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.secret_manager import resolve_scoped_secret_ref
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.signal_templates import (
    format_stage_discord_update,
    format_stage_jira_update,
)
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
from orchestrator.core.worker_stage_notifier import RunStageNotifier
from orchestrator.core.workflow_runner import WorkflowRequest, WorkflowRunner
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.storage.run_queue_events import (
    RUN_QUEUE_NOTIFY_CHANNEL,
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)
from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig, JiraOAuthError

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
JIRA_STAGE_COMMENT_EVENTS = {"decision_gate_required", "run_failed"}


def _resolve_required_secret(
    session: Session,
    *,
    ref_name: str,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:  # noqa: ANN001
    value = resolve_scoped_secret_ref(
        session,
        secret_ref=ref_name,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    if not value:
        raise ValueError(f"Missing secret value for ref '{ref_name}'")
    return value


def _jira_oauth_client(
    *,
    session: Session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> JiraOAuthClient:  # noqa: ANN001
    client_id = _resolve_required_secret(
        session,
        ref_name=settings.jira_oauth_client_id_ref,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    client_secret = _resolve_required_secret(
        session,
        ref_name=settings.jira_oauth_client_secret_ref,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    redirect_uri = f"{settings.public_api_base_url.rstrip('/')}/api/admin/jira/connect/callback"
    return JiraOAuthClient(
        JiraOAuthClientConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
        )
    )


def _refresh_jira_connection_tokens(
    session: Session,
    *,
    connection: JiraOAuthConnection,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    if connection.access_token_expires_at - now > timedelta(seconds=60):
        return decrypt_value(
            ciphertext=connection.access_token_encrypted,
            encryption_key=settings.secrets_encryption_key,
        )

    client = _jira_oauth_client(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    refresh_token = decrypt_value(
        ciphertext=connection.refresh_token_encrypted,
        encryption_key=settings.secrets_encryption_key,
    )
    token_set = client.refresh_tokens(refresh_token=refresh_token)
    connection.access_token_encrypted = encrypt_value(
        plaintext=token_set.access_token,
        encryption_key=settings.secrets_encryption_key,
    )
    connection.refresh_token_encrypted = encrypt_value(
        plaintext=token_set.refresh_token,
        encryption_key=settings.secrets_encryption_key,
    )
    connection.access_token_expires_at = token_set.expires_at
    connection.scopes = token_set.scopes
    connection.updated_at = now
    session.commit()
    return token_set.access_token


def _send_stage_update_to_jira(
    *,
    session: Session,
    tenant: Tenant,
    issue_key: str | None,
    stage: str,
    message: str,
    settings,
) -> None:  # noqa: ANN001
    if not issue_key or not message.strip():
        return
    if stage not in JIRA_STAGE_COMMENT_EVENTS:
        return

    jira_config = tenant.jira_config or {}
    connection_id = str(jira_config.get("connection_id") or "").strip()
    if not connection_id:
        logger.info(
            "worker_jira_stage_update_not_sent tenant_id=%s issue_key=%s stage=%s reason=missing_connection",
            tenant.tenant_id,
            issue_key,
            stage,
        )
        return

    connection = session.get(JiraOAuthConnection, connection_id)
    if connection is None:
        logger.info(
            "worker_jira_stage_update_not_sent tenant_id=%s issue_key=%s stage=%s reason=connection_not_found",
            tenant.tenant_id,
            issue_key,
            stage,
        )
        return

    try:
        access_token = _refresh_jira_connection_tokens(
            session,
            connection=connection,
            settings=settings,
            tenant_id=tenant.tenant_id,
        )
        client = _jira_oauth_client(session=session, settings=settings, tenant_id=tenant.tenant_id)
        client.add_issue_comment(
            access_token=access_token,
            cloud_id=connection.cloud_id,
            issue_id_or_key=issue_key,
            comment=message,
        )
    except (JiraOAuthError, ValueError) as exc:
        logger.warning(
            "worker_jira_stage_update_send_failed tenant_id=%s issue_key=%s stage=%s error=%s",
            tenant.tenant_id,
            issue_key,
            stage,
            exc,
        )


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
    settings = get_settings()
    selection = select_next_queued_run(
        session,
        queued_status=RUN_STATUS_QUEUED,
        running_status=RUN_STATUS_RUNNING,
        failed_status=RUN_STATUS_FAILED,
    )
    if selection.terminal_run is not None:
        return selection.terminal_run
    if selection.run is None or selection.tenant is None:
        return None
    run = selection.run
    tenant = selection.tenant
    effective_policy = selection.effective_policy or tenant.policy_config

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
        stage_update = {
            "stage": "decision_gate_required",
            "tenant_id": run.tenant_id,
            "issue_key": run.issue_key,
            "run_id": run.run_id,
            "jira_message": format_stage_jira_update(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                stage="decision_gate_required",
                jira_url=f"https://example.atlassian.net/browse/{run.issue_key}",
                error=decision_gate.reason,
                next_steps=decision_gate.questions,
            ),
            "discord_message": format_stage_discord_update(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                stage="decision_gate_required",
                jira_url=f"https://example.atlassian.net/browse/{run.issue_key}",
                error=decision_gate.reason,
                next_steps=decision_gate.questions,
            ),
        }
        send_result = send_tenant_discord_message(
            session=session,
            tenant=tenant,
            project=resolve_project_for_run(session, run=run),
            message=stage_update["discord_message"],
            settings=settings,
            event="decision_gate_required",
            open_thread=True,
            thread_name=f"{run.issue_key}-decision-gate",
            thread_intro="Reply here with GTD details, then run !retry <ISSUE_KEY>.",
        )
        _send_stage_update_to_jira(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage=stage_update["stage"],
            message=stage_update["jira_message"],
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
        run.plan = {
            "succeeded": False,
            "attempts": 0,
            "summary": [],
            "test_guidance": [],
            "pr_url": None,
            "stage_updates": [stage_update],
            "decision_gate": decision_gate.to_payload(),
        }
        return mark_run_terminal(
            session,
            run_id=run.run_id,
            terminal_status=RUN_STATUS_BLOCKED,
            last_error=f"Decision Gate required: {decision_gate.reason}",
        )

    project: Project | None = resolve_project_for_run(session, run=run)
    if project is None:
        return fail_missing_project_mapping(session, run=run)
    if project.is_archived:
        return block_archived_project(session, run=run, project=project)

    notifier = RunStageNotifier(
        session=session,
        tenant=tenant,
        run=run,
        settings=settings,
        project=project,
        send_discord_message=send_tenant_discord_message,
        send_jira_message=_send_stage_update_to_jira,
    )
    start_run(session, run=run)

    bind_run_project(session, run=run, project=project)
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config,
        project_overrides=project.policy_overrides,
    )

    try:
        workflow_request = _workflow_request_for_run(
            tenant,
            run,
            project=project,
            effective_policy=effective_policy,
        )
    except (PermissionError, ValueError) as exc:
        return fail_guardrail_violation(session, run=run, error=str(exc))

    jira_issue_url = (
        f"https://example.atlassian.net/browse/{run.issue_key}"
        if run.issue_key
        else None
    )
    notifier.append(
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
        return finalize_cancelled_run(session, run=run, stage_updates=notifier.stage_updates)
    if workflow_result.plan is not None:
        notifier.append(
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
        notifier.append(
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
        notifier.append(
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

    return finalize_workflow_result(
        session,
        run=run,
        workflow_result=workflow_result,
        stage_updates=notifier.stage_updates,
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
