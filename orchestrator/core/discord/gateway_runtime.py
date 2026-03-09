from __future__ import annotations

import logging
import signal
import threading

from orchestrator.api.discord.ingress.executor import register_discord_command_executor
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.discord.gateway_listener import DiscordGatewayListener
from orchestrator.core.logging import configure_logging
from orchestrator.storage.run_queue_events import (
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)

try:
    import psycopg
except ImportError:  # pragma: no cover - dependency is required at runtime
    psycopg = None


logger = logging.getLogger("orchestrator.discord_gateway_runtime")


class DiscordGatewayDependencyFailure(RuntimeError):
    pass


def _try_acquire_leader_lock(*, conn, lock_key: int) -> bool:  # noqa: ANN001
    with conn.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
        row = cursor.fetchone()
    return bool(row and row[0] is True)


def _leader_lock_healthcheck(*, conn) -> bool:  # noqa: ANN001
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
        row = cursor.fetchone()
    return bool(row and row[0] == 1)


def run_discord_gateway() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    register_discord_command_executor()
    _run_gateway_leader_loop(settings=settings)


def _run_gateway_leader_loop(*, settings: Settings) -> None:
    if not is_postgres_database_url(settings.database_url):
        raise DiscordGatewayDependencyFailure(
            "Discord gateway service requires PostgreSQL advisory locks; "
            "set ORCHESTRATOR_DATABASE_URL to a postgresql URL."
        )
    if psycopg is None:
        raise DiscordGatewayDependencyFailure(
            "Discord gateway service requires psycopg to coordinate leader lock."
        )

    stop_event = threading.Event()
    lock_key = int(getattr(settings, "discord_gateway_lock_key", 947102033127))
    poll_seconds = max(1, int(getattr(settings, "discord_gateway_poll_seconds", 3)))
    dsn = postgres_dsn_from_database_url(settings.database_url)
    listener = DiscordGatewayListener(settings=settings)

    def _request_stop() -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, lambda _sig, _frame: _request_stop())
    signal.signal(signal.SIGTERM, lambda _sig, _frame: _request_stop())

    logger.info("discord_gateway_runtime_started lock_key=%s poll_seconds=%s", lock_key, poll_seconds)
    while not stop_event.is_set():
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                acquired = _try_acquire_leader_lock(conn=conn, lock_key=lock_key)
                if not acquired:
                    logger.debug("discord_gateway_leader_waiting lock_key=%s", lock_key)
                    stop_event.wait(timeout=poll_seconds)
                    continue

                logger.info("discord_gateway_leader_acquired lock_key=%s", lock_key)
                listener.start()
                try:
                    while not stop_event.is_set():
                        try:
                            _leader_lock_healthcheck(conn=conn)
                        except Exception as exc:  # noqa: BLE001
                            logger.exception(
                                "discord_gateway_leader_lock_lost lock_key=%s error=%s",
                                lock_key,
                                exc,
                            )
                            break
                        stop_event.wait(timeout=poll_seconds)
                finally:
                    listener.stop()
                    logger.info("discord_gateway_leader_released lock_key=%s", lock_key)
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord_gateway_runtime_loop_failed error=%s", exc)
            stop_event.wait(timeout=poll_seconds)

    listener.stop()
    logger.info("discord_gateway_runtime_stopped")
