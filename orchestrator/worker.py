from __future__ import annotations

import asyncio
import logging
import signal

from sqlalchemy import select

from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.config import get_settings
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.logging import configure_logging
from orchestrator.core.platform_metrics import platform_metrics
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.worker.execution_service import (
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


class WorkerDependencyFailure(RuntimeError):
    pass


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
        try:
            runner = build_workflow_runner_for_session(session=session)
        except CodexRuntimeError as exc:
            platform_metrics.record_worker_failure(kind="dependency")
            raise WorkerDependencyFailure(f"Worker runtime unavailable: {exc}") from exc
        return process_next_queued_run(session, runner)


async def _run_worker_slot(*, session_factory, stop_event: asyncio.Event) -> None:  # noqa: ANN001
    while not stop_event.is_set():
        processed = await asyncio.to_thread(
            _process_next_queued_run_once,
            session_factory=session_factory,
        )
        if processed is None:
            return


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
        listener.stop()
        logger.info("worker_stopped")


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
