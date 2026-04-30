from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable

try:
    import psycopg
except Exception:  # pragma: no cover - import failure is surfaced at runtime.
    psycopg = None

from orchestrator.core.config import get_settings
from orchestrator.storage.run_queue_events import is_postgres_database_url, postgres_dsn_from_database_url

PRODUCT_EVENT_NOTIFY_CHANNEL = "product_event_stream"

logger = logging.getLogger(__name__)

_publisher_lock = threading.Lock()
_publisher_conn = None
_listener_condition = threading.Condition()
_listener_thread: threading.Thread | None = None
_listener_generation = 0
_listener_stop = threading.Event()


def _postgres_dsn() -> str:
    database_url = str(get_settings().database_url or "").strip()
    if not is_postgres_database_url(database_url):
        raise RuntimeError("Product event streaming requires a PostgreSQL database URL for LISTEN/NOTIFY")
    return postgres_dsn_from_database_url(database_url)


def _publisher_connection():
    global _publisher_conn
    if psycopg is None:
        raise RuntimeError("Product event streaming requires psycopg")
    if _publisher_conn is None or getattr(_publisher_conn, "closed", True):
        _publisher_conn = psycopg.connect(_postgres_dsn(), autocommit=True)
    return _publisher_conn


def publish_product_event_notification(
    *,
    event_class: str,
    event_sequence: int,
    tenant_id: str,
    workflow_id: str | None,
    run_id: str | None,
    operation_id: str | None,
    attempt_id: str | None,
) -> None:
    global _publisher_conn
    payload = {
        "event_class": event_class,
        "event_sequence": int(event_sequence),
        "tenant_id": tenant_id,
        "workflow_id": workflow_id,
        "run_id": run_id,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
    }
    serialized = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    with _publisher_lock:
        try:
            _publisher_connection().execute(
                "SELECT pg_notify(%s, %s)",
                (PRODUCT_EVENT_NOTIFY_CHANNEL, serialized),
            )
        except Exception:
            if _publisher_conn is not None:
                try:
                    _publisher_conn.close()
                except Exception:
                    pass
                _publisher_conn = None
            logger.exception(
                "product_event_notify_failed event_class=%s event_sequence=%s tenant_id=%s workflow_id=%s run_id=%s operation_id=%s attempt_id=%s",
                event_class,
                event_sequence,
                tenant_id,
                workflow_id,
                run_id,
                operation_id,
                attempt_id,
            )
            raise


def open_product_event_listener():
    if psycopg is None:
        raise RuntimeError("Product event streaming requires psycopg")
    conn = psycopg.connect(_postgres_dsn(), autocommit=True)
    conn.execute(f'LISTEN "{PRODUCT_EVENT_NOTIFY_CHANNEL}"')
    return conn


def _advance_listener_generation() -> None:
    global _listener_generation
    with _listener_condition:
        _listener_generation += 1
        _listener_condition.notify_all()


def _listener_loop(listener_factory: Callable = open_product_event_listener) -> None:
    while not _listener_stop.is_set():
        try:
            with listener_factory() as conn:
                while not _listener_stop.is_set():
                    received = False
                    for _notification in conn.notifies(timeout=25, stop_after=1):
                        received = True
                    if received:
                        _advance_listener_generation()
        except Exception:
            logger.exception("product_event_listener_failed")
            _advance_listener_generation()
            _listener_stop.wait(timeout=1.0)


def _ensure_listener_thread() -> None:
    global _listener_thread
    with _listener_condition:
        if _listener_thread is not None and _listener_thread.is_alive():
            return
        _listener_stop.clear()
        _listener_thread = threading.Thread(
            target=_listener_loop,
            name="product-event-listener",
            daemon=True,
        )
        _listener_thread.start()


def current_product_event_notification_marker() -> int:
    _ensure_listener_thread()
    with _listener_condition:
        return _listener_generation


def wait_for_product_event_notification(*, marker: int, timeout_seconds: float) -> int:
    _ensure_listener_thread()
    with _listener_condition:
        if _listener_generation <= marker:
            _listener_condition.wait_for(lambda: _listener_generation > marker, timeout=timeout_seconds)
        return _listener_generation
