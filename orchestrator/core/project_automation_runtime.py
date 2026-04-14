from __future__ import annotations

import logging
import os
import signal
import threading
from datetime import UTC, datetime

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.logging import configure_logging
from orchestrator.core.project_automation_service import enqueue_due_project_automation_runs
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.run_queue_events import is_postgres_database_url, postgres_dsn_from_database_url

try:
    import psycopg
except ImportError:  # pragma: no cover
    psycopg = None

logger = logging.getLogger("orchestrator.project_automation_runtime")


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


def _service_instance_id() -> str:
    return f"{os.uname().nodename}:{os.getpid()}"


class ProjectAutomationRuntime:
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings
        self._session_factory = create_session_factory(settings.database_url)
        self._service_instance_id = _service_instance_id()

    def run_forever(self) -> None:
        if not is_postgres_database_url(self._settings.database_url):
            raise RuntimeError(
                "Project automation runtime requires PostgreSQL advisory locks; "
                "set ORCHESTRATOR_DATABASE_URL to a postgresql URL."
            )
        if psycopg is None:
            raise RuntimeError("project_automation runtime requires psycopg")

        stop_event = threading.Event()
        lock_key = int(getattr(self._settings, "project_automation_lock_key", 947102033131))
        poll_seconds = max(5, int(getattr(self._settings, "project_automation_poll_seconds", 30)))
        interval_seconds = max(10, int(getattr(self._settings, "project_automation_interval_seconds", 30)))
        dsn = postgres_dsn_from_database_url(self._settings.database_url)

        def _request_stop() -> None:
            stop_event.set()

        signal.signal(signal.SIGINT, lambda _sig, _frame: _request_stop())
        signal.signal(signal.SIGTERM, lambda _sig, _frame: _request_stop())

        logger.info(
            "project_automation_runtime_started lock_key=%s poll_seconds=%s interval_seconds=%s instance=%s",
            lock_key,
            poll_seconds,
            interval_seconds,
            self._service_instance_id,
        )
        while not stop_event.is_set():
            try:
                with psycopg.connect(dsn, autocommit=True) as conn:
                    if not _try_acquire_leader_lock(conn=conn, lock_key=lock_key):
                        stop_event.wait(timeout=poll_seconds)
                        continue
                    logger.info("project_automation_leader_acquired lock_key=%s", lock_key)
                    while not stop_event.is_set():
                        self._run_scheduler_pass()
                        waited = 0
                        while waited < interval_seconds and not stop_event.is_set():
                            _leader_lock_healthcheck(conn=conn)
                            step = min(poll_seconds, interval_seconds - waited)
                            stop_event.wait(timeout=step)
                            waited += step
                    logger.info("project_automation_leader_released lock_key=%s", lock_key)
            except Exception as exc:  # noqa: BLE001
                logger.exception("project_automation_runtime_loop_failed error=%s", exc)
                stop_event.wait(timeout=poll_seconds)
        logger.info("project_automation_runtime_stopped")

    def _run_scheduler_pass(self) -> None:
        now = datetime.now(UTC)
        with self._session_factory() as session:
            scheduled = enqueue_due_project_automation_runs(session=session, now=now)
        if scheduled:
            logger.info("project_automation_runtime_scheduled count=%s", len(scheduled))


def run_project_automation_runtime() -> None:
    settings = get_settings()
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id="project-automation-runtime",
    )
    ProjectAutomationRuntime(settings=settings).run_forever()
