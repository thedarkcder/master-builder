from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.config import get_settings
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.logging import configure_logging
from orchestrator.core.platform_metrics import platform_metrics
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.worker.run_health import (
    cleanup_orphan_run_locks,
    recover_stale_running_runs,
    worker_service_instance_id,
)
from orchestrator.core.worker.execution_service import (
    process_next_webhook_job_with_dependencies as _process_next_webhook_job_with_dependencies,
    process_next_queued_run_with_dependencies as _process_next_queued_run_with_dependencies,
)
from orchestrator.core.worker.queue_listener import (
    RunQueueNotificationBridge,
    wait_for_wake_or_stop,
)
from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.run_queue_events import (
    RUN_QUEUE_NOTIFY_CHANNEL,
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)
from orchestrator.storage.models import Project, Tenant

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None

logger = logging.getLogger(__name__)
WORKER_DATABASE_RETRY_DELAY_SECONDS = 2.0


class WorkerDependencyFailure(RuntimeError):
    pass


def _is_retryable_database_error(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    if getattr(exc, "connection_invalidated", False):
        return True
    error_text = str(getattr(exc, "orig", exc) or "").lower()
    retryable_markers = (
        "server closed the connection unexpectedly",
        "the database system is shutting down",
        "terminating connection due to administrator command",
        "connection refused",
        "connection not open",
        "connection already closed",
    )
    return any(marker in error_text for marker in retryable_markers)


def _coerce_parallel_slots(raw_value: object) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 1
    return max(1, parsed)


def _resolve_parallel_slots_from_policy(*, session_factory) -> int:  # noqa: ANN001
    try:
        with session_factory() as session:
            tenants = session.execute(
                select(Tenant).where(Tenant.is_enabled.is_(True))
            ).scalars().all()
            if not tenants:
                return 1
            projects = session.execute(
                select(Project).where(Project.is_archived.is_(False))
            ).scalars().all()
    except Exception as exc:
        logger.exception("worker_parallel_slot_resolution_failed error=%s", exc)
        return 1

    projects_by_tenant: dict[str, list[Project]] = {}
    for project in projects:
        projects_by_tenant.setdefault(project.tenant_id, []).append(project)

    max_slots = 1
    for tenant in tenants:
        tenant_policy = tenant.policy_config if isinstance(tenant.policy_config, dict) else {}
        tenant_slots = _coerce_parallel_slots(tenant_policy.get("max_concurrent_runs"))
        max_slots = max(max_slots, tenant_slots)
        for project in projects_by_tenant.get(tenant.tenant_id, []):
            project_overrides = project.policy_overrides if isinstance(project.policy_overrides, dict) else {}
            effective_policy = resolve_effective_policy(
                tenant_policy=tenant_policy,
                project_overrides=project_overrides,
            )
            project_slots = _coerce_parallel_slots(effective_policy.get("max_concurrent_runs"))
            max_slots = max(max_slots, project_slots)
    return max_slots


def process_next_queued_run(session, runner):  # noqa: ANN001
    return _process_next_queued_run_with_dependencies(
        session=session,
        runner=runner,
        send_discord_message_fn=send_tenant_discord_message,
    )


def _process_next_queued_run_once(*, session_factory):  # noqa: ANN001
    with session_factory() as session:
        webhook_job = _process_next_webhook_job_with_dependencies(session=session)
        if webhook_job is not None:
            return webhook_job
        try:
            runner = build_workflow_runner_for_session(session=session)
        except CodexRuntimeError as exc:
            platform_metrics.record_worker_failure(kind="dependency")
            raise WorkerDependencyFailure(f"Worker runtime unavailable: {exc}") from exc
        return process_next_queued_run(session, runner)


async def _run_worker_slot(*, session_factory, stop_event: asyncio.Event) -> None:  # noqa: ANN001
    while not stop_event.is_set():
        try:
            processed = await asyncio.to_thread(
                _process_next_queued_run_once,
                session_factory=session_factory,
            )
        except WorkerDependencyFailure:
            raise
        except Exception as exc:
            if not _is_retryable_database_error(exc):
                raise
            logger.warning(
                "worker_slot_database_retry_scheduled error=%s retry_delay_seconds=%s",
                exc,
                WORKER_DATABASE_RETRY_DELAY_SECONDS,
            )
            if not await _wait_for_worker_retry_delay(
                stop_event=stop_event,
                timeout_seconds=WORKER_DATABASE_RETRY_DELAY_SECONDS,
            ):
                continue
            return
        if processed is None:
            return


async def _wait_for_worker_retry_delay(*, stop_event: asyncio.Event, timeout_seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return False
    return True


def _recover_worker_run_health_once(*, session_factory, settings, agent_id: str, service_instance_id: str) -> None:  # noqa: ANN001
    with session_factory() as session:
        orphaned_locks = cleanup_orphan_run_locks(session=session)
        recovered = recover_stale_running_runs(
            session=session,
            settings=settings,
            recovered_by_agent_id=agent_id,
            recovered_by_service_instance_id=service_instance_id,
        )
    if orphaned_locks:
        logger.info("worker_orphan_lock_cleanup_completed removed=%s", orphaned_locks)
    for item in recovered:
        logger.warning(
            "worker_stale_run_recovered run_id=%s tenant_id=%s issue_key=%s previous_owner=%s last_heartbeat_at=%s",
            item.run_id,
            item.tenant_id,
            item.issue_key,
            item.previous_owner,
            item.last_heartbeat_at.isoformat() if item.last_heartbeat_at is not None else None,
        )


async def _run_stale_recovery_loop(*, session_factory, settings, stop_event: asyncio.Event, agent_id: str, service_instance_id: str) -> None:  # noqa: ANN001
    interval_seconds = max(15, int(getattr(settings, "worker_stale_sweep_interval_seconds", 60)))
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(
                _recover_worker_run_health_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
            )
        except Exception:
            logger.exception("worker_stale_recovery_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id=str(settings.agent_id or "").strip() or "worker",
    )
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
    stale_recovery_task: asyncio.Task[None] | None = None
    listener.start()
    service_instance_id = worker_service_instance_id()
    agent_id = str(settings.agent_id or "").strip() or "worker"
    await asyncio.to_thread(
        _recover_worker_run_health_once,
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
    )
    stale_recovery_task = asyncio.create_task(
        _run_stale_recovery_loop(
            session_factory=session_factory,
            settings=settings,
            stop_event=stop_event,
            agent_id=agent_id,
            service_instance_id=service_instance_id,
        )
    )

    logger.info("worker_started")
    slots: list[asyncio.Task[None]] = []
    try:
        while not stop_event.is_set():
            await wait_for_wake_or_stop(wake_event=wake_event, stop_event=stop_event)
            if stop_event.is_set():
                break
            wake_event.clear()
            parallel_slots = _resolve_parallel_slots_from_policy(session_factory=session_factory)
            while len(slots) < parallel_slots:
                slots.append(
                    asyncio.create_task(
                        _run_worker_slot(session_factory=session_factory, stop_event=stop_event)
                    )
                )
            while slots and not stop_event.is_set():
                done, pending = await asyncio.wait(
                    slots,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                slots = list(pending)
                for task in done:
                    task.result()
                if stop_event.is_set():
                    break
                if wake_event.is_set():
                    wake_event.clear()
                    parallel_slots = _resolve_parallel_slots_from_policy(session_factory=session_factory)
                    while len(slots) < parallel_slots:
                        slots.append(
                            asyncio.create_task(
                                _run_worker_slot(session_factory=session_factory, stop_event=stop_event)
                            )
                        )
    except WorkerDependencyFailure:
        raise
    except Exception:
        platform_metrics.record_worker_failure(kind="crash")
        raise
    finally:
        for task in slots:
            task.cancel()
        if slots:
            await asyncio.gather(*slots, return_exceptions=True)
        if stale_recovery_task is not None:
            stale_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await stale_recovery_task
        listener.stop()
        logger.info("worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
