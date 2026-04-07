from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.api.admin.tenant_crud import purge_expired_archived_tenants
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.logging import configure_logging
from orchestrator.core.platform_metrics import platform_metrics
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import RUN_STATUS_RUNNING
from orchestrator.core.worker.run_health import (
    recover_stale_running_runs,
    worker_service_instance_id_for_mode,
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
from orchestrator.core.worker_capabilities import parse_worker_capabilities
from orchestrator.core.workflow.runner import WorkflowRunner
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.run_queue_events import (
    RUN_QUEUE_NOTIFY_CHANNEL,
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)
from orchestrator.storage.models import Project, Run, Tenant, WorkerRuntimeState

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None

logger = logging.getLogger(__name__)
WORKER_DATABASE_RETRY_DELAY_SECONDS = 2.0
WORKER_RUNTIME_HEARTBEAT_INTERVAL_SECONDS = 30
WORKER_MODE_RUNS = "runs"
WORKER_MODE_WEBHOOKS = "webhooks"


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


def _coerce_non_negative_int(raw_value: object) -> int:
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return 0
    return max(0, parsed)


def _worker_runtime_capabilities(*, settings: Settings) -> list[str]:
    return sorted(parse_worker_capabilities(getattr(settings, "worker_capabilities", None)))


def _worker_runtime_active_run_count(
    *,
    session: Session,
    service_instance_id: str,
) -> int:
    count = session.execute(
        select(func.count(Run.run_id)).where(
            Run.status == RUN_STATUS_RUNNING,
            Run.worker_service_instance_id == service_instance_id,
        )
    ).scalar_one()
    return _coerce_non_negative_int(count)


def _upsert_worker_runtime_state_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
    state: str,
) -> None:
    now = datetime.now(timezone.utc)
    capabilities = _worker_runtime_capabilities(settings=settings)
    with session_factory() as session:
        row = session.get(WorkerRuntimeState, service_instance_id)
        if row is None:
            row = WorkerRuntimeState(
                service_instance_id=service_instance_id,
                agent_id=agent_id,
                worker_mode=worker_mode,
                capabilities_json=capabilities,
                state=state,
                started_at=now,
                last_heartbeat_at=now,
                updated_at=now,
            )
            session.add(row)
        else:
            row.agent_id = agent_id
            row.worker_mode = worker_mode
            row.capabilities_json = capabilities
            row.state = state
            row.last_heartbeat_at = now
            row.updated_at = now
            if row.started_at is None:
                row.started_at = now
        session.commit()


def _register_worker_runtime_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=worker_mode,
        state="starting",
    )


def _refresh_worker_runtime_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    with session_factory() as session:
        active_run_count = _worker_runtime_active_run_count(session=session, service_instance_id=service_instance_id)
    state = "busy" if active_run_count > 0 else "idle"
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=worker_mode,
        state=state,
    )


def _stop_worker_runtime_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    _upsert_worker_runtime_state_once(
        session_factory=session_factory,
        settings=settings,
        agent_id=agent_id,
        service_instance_id=service_instance_id,
        worker_mode=worker_mode,
        state="stopped",
    )


def _resolve_parallel_slots_from_policy(*, session_factory: sessionmaker[Session]) -> int:
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


def process_next_queued_run(session: Session, runner: WorkflowRunner) -> object | None:
    return _process_next_queued_run_with_dependencies(
        session=session,
        runner=runner,
        send_discord_message_fn=send_tenant_discord_message,
    )


def _process_next_webhook_job_once(*, session_factory: sessionmaker[Session]) -> object | None:
    return _process_next_webhook_job_with_dependencies(session_factory=session_factory)


def _process_next_run_once(*, session_factory: sessionmaker[Session]) -> object | None:
    with session_factory() as session:
        try:
            runner = build_workflow_runner_for_session(session=session)
        except CodexRuntimeError as exc:
            platform_metrics.record_worker_failure(kind="dependency")
            raise WorkerDependencyFailure(f"Worker runtime unavailable: {exc}") from exc
        return process_next_queued_run(session, runner)


def _resolve_worker_processor(*, mode: str) -> Callable[..., object | None]:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == WORKER_MODE_RUNS:
        return _process_next_run_once
    if normalized_mode == WORKER_MODE_WEBHOOKS:
        return _process_next_webhook_job_once
    raise ValueError(f"Unsupported worker mode '{mode}'")


async def _run_worker_slot(
    *,
    session_factory: sessionmaker[Session],
    stop_event: asyncio.Event,
    process_next_work_item_once: Callable[..., object | None],
) -> None:
    while not stop_event.is_set():
        try:
            processed = await asyncio.to_thread(
                process_next_work_item_once,
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


def _recover_worker_run_health_once(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    agent_id: str,
    service_instance_id: str,
) -> None:
    with session_factory() as session:
        recovered = recover_stale_running_runs(
            session=session,
            settings=settings,
            recovered_by_agent_id=agent_id,
            recovered_by_service_instance_id=service_instance_id,
        )
    for item in recovered:
        logger.warning(
            "worker_stale_run_recovered run_id=%s tenant_id=%s issue_key=%s previous_owner=%s last_heartbeat_at=%s",
            item.run_id,
            item.tenant_id,
            item.issue_key,
            item.previous_owner,
            item.last_heartbeat_at.isoformat() if item.last_heartbeat_at is not None else None,
        )


def _purge_archived_tenants_once(
    *,
    session_factory: sessionmaker[Session],
) -> int:
    with session_factory() as session:
        purged = purge_expired_archived_tenants(session=session)
    if purged:
        logger.warning("worker_archived_tenant_purge_completed deleted=%s", purged)
    return purged


async def _run_stale_recovery_loop(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    stop_event: asyncio.Event,
    agent_id: str,
    service_instance_id: str,
) -> None:
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


async def _run_archived_tenant_purge_loop(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    stop_event: asyncio.Event,
) -> None:
    interval_seconds = max(60, int(getattr(settings, "tenant_archive_sweep_interval_seconds", 3600)))
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(
                _purge_archived_tenants_once,
                session_factory=session_factory,
            )
        except Exception:
            logger.exception("worker_archived_tenant_purge_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def _run_worker_runtime_heartbeat_loop(
    *,
    session_factory: sessionmaker[Session],
    settings: Settings,
    stop_event: asyncio.Event,
    agent_id: str,
    service_instance_id: str,
    worker_mode: str,
) -> None:
    interval_seconds = max(
        10,
        int(getattr(settings, "worker_runtime_heartbeat_interval_seconds", WORKER_RUNTIME_HEARTBEAT_INTERVAL_SECONDS)),
    )
    while not stop_event.is_set():
        try:
            await asyncio.to_thread(
                _refresh_worker_runtime_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
                worker_mode=worker_mode,
            )
        except Exception:
            logger.exception("worker_runtime_heartbeat_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            continue


async def run_worker(*, mode: str = WORKER_MODE_RUNS) -> None:
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
    process_next_work_item_once = _resolve_worker_processor(mode=mode)
    listener = RunQueueNotificationBridge(
        postgres_dsn=postgres_dsn_from_database_url(settings.database_url),
        wake_event=wake_event,
        loop=loop,
        logger=logger,
        notify_channel=RUN_QUEUE_NOTIFY_CHANNEL,
        psycopg_module=psycopg,
    )
    stale_recovery_task: asyncio.Task[None] | None = None
    archived_tenant_purge_task: asyncio.Task[None] | None = None
    worker_runtime_heartbeat_task: asyncio.Task[None] | None = None
    service_instance_id = worker_service_instance_id_for_mode(settings=settings, mode=mode)
    agent_id = str(settings.agent_id or "").strip() or "worker"
    slots: list[asyncio.Task[None]] = []
    try:
        listener.start()
        await asyncio.to_thread(
            _register_worker_runtime_once,
            session_factory=session_factory,
            settings=settings,
            agent_id=agent_id,
            service_instance_id=service_instance_id,
            worker_mode=mode,
        )
        worker_runtime_heartbeat_task = asyncio.create_task(
            _run_worker_runtime_heartbeat_loop(
                session_factory=session_factory,
                settings=settings,
                stop_event=stop_event,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
                worker_mode=mode,
            )
        )
        if mode == WORKER_MODE_RUNS:
            await asyncio.to_thread(
                _recover_worker_run_health_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
            )
            await asyncio.to_thread(
                _purge_archived_tenants_once,
                session_factory=session_factory,
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
            archived_tenant_purge_task = asyncio.create_task(
                _run_archived_tenant_purge_loop(
                    session_factory=session_factory,
                    settings=settings,
                    stop_event=stop_event,
                )
            )

        logger.info("worker_started mode=%s", mode)
        while not stop_event.is_set():
            await wait_for_wake_or_stop(wake_event=wake_event, stop_event=stop_event)
            if stop_event.is_set():
                break
            wake_event.clear()
            parallel_slots = _resolve_parallel_slots_from_policy(session_factory=session_factory)
            while len(slots) < parallel_slots:
                slots.append(
                    asyncio.create_task(
                        _run_worker_slot(
                            session_factory=session_factory,
                            stop_event=stop_event,
                            process_next_work_item_once=process_next_work_item_once,
                        )
                    )
                )
            while slots and not stop_event.is_set():
                done, pending = await asyncio.wait(
                    slots,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                slots = list(pending)
                slot_failed = False
                for task in done:
                    try:
                        task.result()
                    except WorkerDependencyFailure:
                        raise
                    except Exception as exc:
                        slot_failed = True
                        platform_metrics.record_worker_failure(kind="slot_crash")
                        logger.exception("worker_slot_failed error=%s", exc)
                if slot_failed and not stop_event.is_set():
                    wake_event.set()
                if stop_event.is_set():
                    break
                if wake_event.is_set():
                    wake_event.clear()
                    parallel_slots = _resolve_parallel_slots_from_policy(session_factory=session_factory)
                    while len(slots) < parallel_slots:
                        slots.append(
                            asyncio.create_task(
                                _run_worker_slot(
                                    session_factory=session_factory,
                                    stop_event=stop_event,
                                    process_next_work_item_once=process_next_work_item_once,
                                )
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
        if archived_tenant_purge_task is not None:
            archived_tenant_purge_task.cancel()
            with suppress(asyncio.CancelledError):
                await archived_tenant_purge_task
        if worker_runtime_heartbeat_task is not None:
            worker_runtime_heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await worker_runtime_heartbeat_task
        with suppress(Exception):
            await asyncio.to_thread(
                _stop_worker_runtime_once,
                session_factory=session_factory,
                settings=settings,
                agent_id=agent_id,
                service_instance_id=service_instance_id,
                worker_mode=mode,
            )
        listener.stop()
        logger.info("worker_stopped mode=%s", mode)


def main(*, mode: str = WORKER_MODE_RUNS) -> None:
    asyncio.run(run_worker(mode=mode))


if __name__ == "__main__":
    main()
