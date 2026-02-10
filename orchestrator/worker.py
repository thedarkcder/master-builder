from __future__ import annotations

import asyncio
import logging
import signal

from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.config import get_settings
from orchestrator.core.discord_notifications import send_tenant_discord_message
from orchestrator.core.logging import configure_logging
from orchestrator.core.worker_execution_service import (
    process_next_queued_run_with_dependencies as _process_next_queued_run_with_dependencies,
)
from orchestrator.core.worker_queue_listener import (
    RunQueueNotificationBridge,
    wait_for_wake_or_stop,
)
from orchestrator.core.worker_runtime_factory import build_workflow_runner_for_session
from orchestrator.storage.db import create_session_factory
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


def process_next_queued_run(session, runner):  # noqa: ANN001
    return _process_next_queued_run_with_dependencies(
        session=session,
        runner=runner,
        send_discord_message_fn=send_tenant_discord_message,
    )


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
                        runner = build_workflow_runner_for_session(session=session)
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
