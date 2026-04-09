from __future__ import annotations

import asyncio
import contextlib
import threading

RECONNECT_DELAY_SECONDS = 2.0


class RunQueueNotificationBridge:
    def __init__(
        self,
        *,
        postgres_dsn: str,
        wake_event: asyncio.Event,
        loop: asyncio.AbstractEventLoop,
        logger,
        notify_channel: str,
        psycopg_module,
    ) -> None:  # noqa: ANN001
        self._postgres_dsn = postgres_dsn
        self._wake_event = wake_event
        self._loop = loop
        self._logger = logger
        self._notify_channel = notify_channel
        self._psycopg = psycopg_module
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
                try:
                    self._conn.close()
                except Exception as exc:
                    self._logger.exception("worker_queue_listener_close_failed error=%s", exc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._run_once()

    def _run_once(self) -> None:
        if self._psycopg is None:
            self._logger.error("worker_queue_listener_unavailable reason=missing_psycopg")
            self._loop.call_soon_threadsafe(self._wake_event.set)
            return
        try:
            with self._psycopg.connect(self._postgres_dsn, autocommit=True) as conn:
                with self._conn_lock:
                    self._conn = conn
                conn.execute(f'LISTEN "{self._notify_channel}"')
                # Wake once on startup to drain any queued runs that predate the listener.
                self._loop.call_soon_threadsafe(self._wake_event.set)
                for _notification in conn.notifies():
                    if self._stop_event.is_set():
                        break
                    self._loop.call_soon_threadsafe(self._wake_event.set)
        except Exception as exc:
            if not self._stop_event.is_set():
                self._logger.exception("worker_queue_listener_failed error=%s", exc)
                self._loop.call_soon_threadsafe(self._wake_event.set)
                # Dependency reconnect backoff, not workflow synchronization.
                self._stop_event.wait(RECONNECT_DELAY_SECONDS)
        finally:
            with self._conn_lock:
                self._conn = None


async def wait_for_wake_or_stop(
    *,
    wake_event: asyncio.Event,
    stop_event: asyncio.Event,
    timeout_seconds: float | None = None,
) -> bool:
    if wake_event.is_set() or stop_event.is_set():
        return False
    wake_task = asyncio.create_task(wake_event.wait())
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {wake_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
        timeout=timeout_seconds,
    )
    timed_out = len(done) == 0
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    for task in done:
        with contextlib.suppress(asyncio.CancelledError):
            task.result()
    return timed_out
