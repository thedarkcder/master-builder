from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import threading
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.core.codex_agents import CodexWorkflowAgents
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import evaluate_decision_gate
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.core.enforcement_context import build_agent_enforcement_context
from orchestrator.core.guardrails import enforce_safe_command
from orchestrator.core.logging import configure_logging
from orchestrator.core.secret_manager import resolve_secret_ref
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.signal_templates import (
    format_stage_discord_update,
    format_stage_jira_update,
)
from orchestrator.core.workflow_runner import WorkflowRequest, WorkflowRunner
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import JiraOAuthConnection, Run, Tenant
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


def _resolve_required_secret(session: Session, *, ref_name: str, settings) -> str:  # noqa: ANN001
    value = resolve_secret_ref(
        session,
        secret_ref=ref_name,
        encryption_key=settings.secrets_encryption_key,
    )
    if not value:
        raise ValueError(f"Missing secret value for ref '{ref_name}'")
    return value


def _jira_oauth_client(*, session: Session, settings) -> JiraOAuthClient:  # noqa: ANN001
    client_id = _resolve_required_secret(session, ref_name=settings.jira_oauth_client_id_ref, settings=settings)
    client_secret = _resolve_required_secret(
        session,
        ref_name=settings.jira_oauth_client_secret_ref,
        settings=settings,
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
) -> str:  # noqa: ANN001
    now = datetime.now(timezone.utc)
    if connection.access_token_expires_at - now > timedelta(seconds=60):
        return decrypt_value(
            ciphertext=connection.access_token_encrypted,
            encryption_key=settings.secrets_encryption_key,
        )

    client = _jira_oauth_client(session=session, settings=settings)
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
        )
        client = _jira_oauth_client(session=session, settings=settings)
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

    settings = get_settings()
    enforcement_context = _cached_enforcement_context(settings.required_codex_assets_version or "")

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


@lru_cache(maxsize=1)
def _cached_enforcement_context(required_assets_version: str) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    return build_agent_enforcement_context(
        repo_root=repo_root,
        required_assets_version=required_assets_version or None,
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
            message=stage_update["discord_message"],
            settings=settings,
            event="decision_gate_required",
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
        run.status = RUN_STATUS_BLOCKED
        run.last_error = f"Decision Gate required: {decision_gate.reason}"
        run.plan = {
            "succeeded": False,
            "attempts": 0,
            "summary": [],
            "test_guidance": [],
            "pr_url": None,
            "stage_updates": [stage_update],
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
            event=stage_update["stage"],
        )
        if not send_result.sent:
            logger.info(
                "worker_discord_stage_update_not_sent tenant_id=%s run_id=%s stage=%s reason=%s",
                run.tenant_id,
                run.run_id,
                stage_update["stage"],
                send_result.reason,
            )
        _send_stage_update_to_jira(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage=stage_update["stage"],
            message=stage_update["jira_message"],
            settings=settings,
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
