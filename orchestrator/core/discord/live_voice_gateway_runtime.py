from __future__ import annotations

import logging
import signal
import threading

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.discord.live_voice_service import (
    DiscordLiveVoiceDependencyFailure,
    DiscordLiveVoiceService,
)
from orchestrator.core.observability.logging import configure_logging
from orchestrator.core.observability.otel_telemetry import initialize_telemetry
from orchestrator.storage.run_queue_events import (
    is_postgres_database_url,
    postgres_dsn_from_database_url,
)

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None


logger = logging.getLogger("orchestrator.discord_live_voice_runtime")


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


def run_discord_live_voice() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id="discord-live-voice",
    )
    initialize_telemetry(settings=settings, service_name="discord-live-voice")
    _run_live_voice_leader_loop(settings=settings)


def _run_live_voice_leader_loop(*, settings: Settings) -> None:
    if not is_postgres_database_url(settings.database_url):
        raise DiscordLiveVoiceDependencyFailure(
            "Discord live voice service requires PostgreSQL advisory locks; "
            "set ORCHESTRATOR_DATABASE_URL to a postgresql URL."
        )
    if psycopg is None:
        raise DiscordLiveVoiceDependencyFailure(
            "Discord live voice service requires psycopg to coordinate leader lock."
        )

    runtime_stop_event = threading.Event()
    lock_key = int(getattr(settings, "discord_live_voice_lock_key", 947102033129))
    poll_seconds = max(1, int(getattr(settings, "discord_live_voice_poll_seconds", 3)))
    dsn = postgres_dsn_from_database_url(settings.database_url)

    def _request_stop() -> None:
        runtime_stop_event.set()

    signal.signal(signal.SIGINT, lambda _sig, _frame: _request_stop())
    signal.signal(signal.SIGTERM, lambda _sig, _frame: _request_stop())

    logger.info(
        "discord_live_voice_runtime_started lock_key=%s poll_seconds=%s",
        lock_key,
        poll_seconds,
    )
    while not runtime_stop_event.is_set():
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                acquired = _try_acquire_leader_lock(conn=conn, lock_key=lock_key)
                if not acquired:
                    runtime_stop_event.wait(timeout=poll_seconds)
                    continue

                logger.info("discord_live_voice_leader_acquired lock_key=%s", lock_key)
                attempt_stop_event = threading.Event()
                health_thread = threading.Thread(
                    target=_healthcheck_loop,
                    kwargs={
                        "conn": conn,
                        "attempt_stop_event": attempt_stop_event,
                        "runtime_stop_event": runtime_stop_event,
                        "poll_seconds": poll_seconds,
                    },
                    name="discord-live-voice-lock-health",
                    daemon=True,
                )
                try:
                    service = DiscordLiveVoiceService(
                        settings=settings, stop_event=attempt_stop_event
                    )
                    health_thread.start()
                    service.run()
                finally:
                    attempt_stop_event.set()
                    health_thread.join(timeout=max(1, poll_seconds + 1))
                    logger.info(
                        "discord_live_voice_leader_released lock_key=%s", lock_key
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception("discord_live_voice_runtime_loop_failed error=%s", exc)
            runtime_stop_event.wait(timeout=poll_seconds)

    logger.info("discord_live_voice_runtime_stopped")


def _healthcheck_loop(  # noqa: ANN001
    *,
    conn,
    attempt_stop_event: threading.Event,
    runtime_stop_event: threading.Event,
    poll_seconds: int,
) -> None:
    while not attempt_stop_event.is_set() and not runtime_stop_event.is_set():
        try:
            _leader_lock_healthcheck(conn=conn)
        except Exception as exc:  # noqa: BLE001
            if attempt_stop_event.is_set() or runtime_stop_event.is_set():
                return
            logger.exception("discord_live_voice_leader_lock_lost error=%s", exc)
            attempt_stop_event.set()
            return
        if attempt_stop_event.wait(timeout=poll_seconds) or runtime_stop_event.is_set():
            return
