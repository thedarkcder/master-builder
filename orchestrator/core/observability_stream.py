from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
from queue import Empty, Full, Queue
import threading

from sqlalchemy import Select, delete, desc, func, select
from sqlalchemy.event import listens_for
from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.observability_policy import normalize_tenant_observability_policy
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import ObservabilityStreamEvent, Tenant

OBSERVABILITY_STREAM_REDIS_CHANNEL = "observability_stream_events"
_SESSION_OFFSETS_KEY = "_observability_stream_offsets"

try:
    from redis import Redis
    from redis.exceptions import RedisError
except ImportError:  # pragma: no cover - optional in tests
    Redis = None  # type: ignore[assignment]

    class RedisError(Exception):
        pass


@dataclass(frozen=True)
class StreamWakeup:
    min_offset: int
    max_offset: int
    count: int
    tenant_id: str | None
    operation_id: str | None
    attempt_id: str | None


@dataclass
class StreamSubscriber:
    queue: Queue[str]
    match_fn: Callable[[ObservabilityStreamEvent], bool]
    render_fn: Callable[[ObservabilityStreamEvent], str | None]
    min_stream_offset_exclusive: int = 0
    disconnected: bool = False


_redis_client_lock = threading.Lock()
_redis_client: Redis | None = None
_broker_lock = threading.Lock()
_broker: ObservabilityStreamBroker | None = None
_publisher_lock = threading.Lock()
_publisher: ObservabilityStreamPublisher | None = None
_retention_sweeper: ObservabilityStreamRetentionSweeper | None = None


def _make_redis_client(*, timeout_ms: int | None = None) -> Redis | None:
    settings = get_settings()
    redis_url = str(getattr(settings, "redis_url", "") or "").strip()
    if not bool(getattr(settings, "log_bus_enabled", False)) or not redis_url or Redis is None:
        return None
    effective_timeout_ms = timeout_ms
    if effective_timeout_ms is None:
        effective_timeout_ms = max(1, int(getattr(settings, "log_redis_publish_timeout_ms", 10)))
    return Redis.from_url(
        redis_url,
        socket_timeout=effective_timeout_ms / 1000.0,
        socket_connect_timeout=effective_timeout_ms / 1000.0,
        health_check_interval=30,
    )


def _redis_client_instance() -> Redis | None:
    global _redis_client
    with _redis_client_lock:
        if _redis_client is None:
            _redis_client = _make_redis_client()
        return _redis_client


def _reset_redis_client() -> None:
    global _redis_client
    with _redis_client_lock:
        client = _redis_client
        _redis_client = None
    if client is not None:
        try:
            client.close()
        except Exception:
            pass


def _normalized_payload(payload: dict | None) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if isinstance(value, str):
            normalized[normalized_key] = redact_sensitive_text(value)
        elif isinstance(value, dict):
            normalized[normalized_key] = _normalized_payload(value)
        elif isinstance(value, list):
            normalized[normalized_key] = [
                redact_sensitive_text(item) if isinstance(item, str) else item
                for item in value
            ]
        else:
            normalized[normalized_key] = value
    return normalized


def register_observability_stream_offsets(*, session: Session, rows: list[ObservabilityStreamEvent]) -> None:
    if not rows:
        return
    offsets = [int(row.stream_offset) for row in rows if getattr(row, "stream_offset", None) is not None]
    if not offsets:
        return
    bucket = session.info.setdefault(_SESSION_OFFSETS_KEY, [])
    bucket.append(
        StreamWakeup(
            min_offset=min(offsets),
            max_offset=max(offsets),
            count=len(offsets),
            tenant_id=rows[0].tenant_id if rows else None,
            operation_id=rows[0].operation_id if rows else None,
            attempt_id=rows[0].attempt_id if rows else None,
        )
    )


def record_observability_stream_event(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None,
    run_id: str | None,
    operation_id: str | None,
    attempt_id: str | None,
    issue_key: str | None,
    event_kind: str,
    level: str,
    source_component: str | None,
    message: str,
    payload: dict | None = None,
    recorded_at: datetime | None = None,
) -> ObservabilityStreamEvent | None:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_message = redact_sensitive_text(str(message or "").strip())
    normalized_event_kind = str(event_kind or "").strip().lower()
    if not normalized_tenant_id or not normalized_message or not normalized_event_kind:
        return None
    row = ObservabilityStreamEvent(
        tenant_id=normalized_tenant_id,
        project_id=str(project_id or "").strip() or None,
        workflow_id=str(workflow_id or "").strip() or None,
        run_id=str(run_id or "").strip() or None,
        operation_id=str(operation_id or "").strip() or None,
        attempt_id=str(attempt_id or "").strip() or None,
        issue_key=str(issue_key or "").strip() or None,
        event_kind=normalized_event_kind,
        level=str(level or "").strip().lower() or "info",
        source_component=str(source_component or "").strip() or None,
        message=normalized_message,
        payload_json=_normalized_payload(payload),
        recorded_at=recorded_at or datetime.now(timezone.utc),
    )
    session.add(row)
    session.flush()
    register_observability_stream_offsets(session=session, rows=[row])
    return row


def observability_stream_event_to_payload(row: ObservabilityStreamEvent) -> dict[str, object]:
    payload = dict(row.payload_json or {})
    attempt_number: int | None = None
    raw_attempt = payload.get("attempt")
    if isinstance(raw_attempt, int):
        attempt_number = raw_attempt
    elif isinstance(raw_attempt, str):
        try:
            attempt_number = int(raw_attempt)
        except ValueError:
            attempt_number = None
    return {
        "event_id": f"telemetry:{row.stream_offset}",
        "source": "telemetry",
        "level": row.level,
        "event_kind": row.event_kind,
        "message": row.message,
        "source_component": row.source_component,
        "run_id": row.run_id,
        "operation_id": row.operation_id,
        "attempt_id": row.attempt_id,
        "agent_id": str(payload.get("agent_id") or "").strip() or None,
        "invocation_id": str(payload.get("invocation_id") or "").strip() or None,
        "stage": str(payload.get("stage") or "").strip() or None,
        "attempt": attempt_number,
        "stream": str(payload.get("stream") or "").strip() or None,
        "payload": payload,
        "recorded_at": row.recorded_at.isoformat(),
    }


def build_observability_snapshot_query(
    *,
    operation_id: str,
    attempt_id: str | None = None,
    limit: int = 500,
) -> list[ObservabilityStreamEvent]:
    normalized_operation_id = str(operation_id or "").strip()
    if not normalized_operation_id:
        return []
    query: Select[tuple[ObservabilityStreamEvent]] = (
        select(ObservabilityStreamEvent)
        .where(ObservabilityStreamEvent.operation_id == normalized_operation_id)
        .order_by(desc(ObservabilityStreamEvent.stream_offset))
        .limit(max(1, min(limit, 2000)))
    )
    normalized_attempt_id = str(attempt_id or "").strip()
    if normalized_attempt_id:
        query = query.where(ObservabilityStreamEvent.attempt_id == normalized_attempt_id)
    with create_session_factory() as session:
        rows = session.execute(query).scalars().all()
    return list(reversed(rows))


def list_operation_observability_events(
    *,
    session: Session,
    operation_id: str,
    attempt_id: str | None = None,
    limit: int = 500,
) -> list[ObservabilityStreamEvent]:
    normalized_operation_id = str(operation_id or "").strip()
    if not normalized_operation_id:
        return []
    query = (
        select(ObservabilityStreamEvent)
        .where(ObservabilityStreamEvent.operation_id == normalized_operation_id)
        .order_by(desc(ObservabilityStreamEvent.recorded_at), desc(ObservabilityStreamEvent.stream_offset))
        .limit(max(1, min(limit, 2000)))
    )
    normalized_attempt_id = str(attempt_id or "").strip()
    if normalized_attempt_id:
        query = query.where(ObservabilityStreamEvent.attempt_id == normalized_attempt_id)
    rows = session.execute(query).scalars().all()
    return list(reversed(rows))


def _publish_wakeup(message: StreamWakeup) -> None:
    client = _redis_client_instance()
    if client is None:
        return
    payload = json.dumps(
        {
            "min_offset": message.min_offset,
            "max_offset": message.max_offset,
            "count": message.count,
            "tenant_id": message.tenant_id,
            "operation_id": message.operation_id,
            "attempt_id": message.attempt_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        client.publish(OBSERVABILITY_STREAM_REDIS_CHANNEL, payload)
    except RedisError:
        _reset_redis_client()


def _redis_wakeups_enabled() -> bool:
    settings = get_settings()
    redis_url = str(getattr(settings, "redis_url", "") or "").strip()
    return bool(getattr(settings, "log_bus_enabled", False) and redis_url and Redis is not None)


class ObservabilityStreamPublisher:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._stop_event = threading.Event()
        self._started = False
        max_queue_size = max(64, int(getattr(self._settings, "log_db_batch_size", 250)) * 4)
        self._queue: Queue[StreamWakeup] = Queue(maxsize=max_queue_size)
        self._thread = threading.Thread(target=self._run, name="observability-stream-publisher", daemon=True)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._started:
            self._thread.join(timeout=2.0)
        self._started = False

    def enqueue(self, message: StreamWakeup) -> None:
        if self._stop_event.is_set():
            return
        self.start()
        try:
            self._queue.put_nowait(message)
        except Full:
            return

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                _publish_wakeup(message)
            finally:
                self._queue.task_done()


def get_observability_stream_publisher() -> ObservabilityStreamPublisher:
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            _publisher = ObservabilityStreamPublisher()
        return _publisher


def publish_stream_wakeups(messages: list[StreamWakeup]) -> None:
    if not messages or not _redis_wakeups_enabled():
        return
    publisher = get_observability_stream_publisher()
    for message in messages:
        publisher.enqueue(message)


@listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    messages = list(session.info.pop(_SESSION_OFFSETS_KEY, []))
    publish_stream_wakeups(messages)


@listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_SESSION_OFFSETS_KEY, None)


class ObservabilityStreamBroker:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._session_factory = create_session_factory()
        self._subscribers: dict[str, StreamSubscriber] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._started = False
        self._last_seen_offset = 0
        self._redis_connected = False
        self._poll_thread = threading.Thread(target=self._run_poll_loop, name="observability-stream-poll", daemon=True)
        self._redis_thread = threading.Thread(target=self._run_redis_loop, name="observability-stream-redis", daemon=True)
        self._last_seen_offset = self._load_high_water_mark()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._poll_thread.start()
        self._redis_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._started:
            self._poll_thread.join(timeout=2.0)
            self._redis_thread.join(timeout=2.0)
        self._started = False
        self._redis_connected = False

    def subscribe(
        self,
        *,
        subscriber_id: str,
        buffer_size: int,
        match_fn: Callable[[ObservabilityStreamEvent], bool],
        render_fn: Callable[[ObservabilityStreamEvent], str | None],
        min_stream_offset_exclusive: int = 0,
    ) -> StreamSubscriber:
        subscriber = StreamSubscriber(
            queue=Queue(maxsize=max(1, buffer_size)),
            match_fn=match_fn,
            render_fn=render_fn,
            min_stream_offset_exclusive=max(0, int(min_stream_offset_exclusive)),
        )
        with self._lock:
            self._subscribers[subscriber_id] = subscriber
        return subscriber

    def unsubscribe(self, subscriber_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)

    def request_catchup(self) -> None:
        self._wake_event.set()

    def _load_high_water_mark(self) -> int:
        with self._session_factory() as session:
            value = session.execute(select(func.max(ObservabilityStreamEvent.stream_offset))).scalar_one_or_none()
        return int(value or 0)

    def _run_poll_loop(self) -> None:
        poll_seconds = max(0.1, int(getattr(self._settings, "log_broker_poll_ms", 500)) / 1000.0)
        debounce_seconds = max(0.0, int(getattr(self._settings, "log_wake_debounce_ms", 10)) / 1000.0)
        while not self._stop_event.is_set():
            woke = self._wake_event.wait(timeout=poll_seconds)
            self._wake_event.clear()
            if woke and debounce_seconds > 0:
                self._stop_event.wait(timeout=debounce_seconds)
            self._catch_up_once()

    def _run_redis_loop(self) -> None:
        if not bool(getattr(self._settings, "log_bus_enabled", False)):
            return
        redis_url = str(getattr(self._settings, "redis_url", "") or "").strip()
        if not redis_url or Redis is None:
            return
        timeout_seconds = max(0.1, int(getattr(self._settings, "log_broker_poll_ms", 500)) / 1000.0)
        while not self._stop_event.is_set():
            client = _make_redis_client(timeout_ms=max(1000, int(timeout_seconds * 1000) + 250))
            if client is None:
                return
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe(OBSERVABILITY_STREAM_REDIS_CHANNEL)
                self._redis_connected = True
                while not self._stop_event.is_set():
                    message = pubsub.get_message(timeout=timeout_seconds)
                    if message is None:
                        continue
                    self.request_catchup()
            except RedisError:
                pass
            finally:
                self._redis_connected = False
                try:
                    pubsub.close()
                except Exception:
                    pass
                try:
                    client.close()
                except Exception:
                    pass
                self._stop_event.wait(timeout=1.0)

    def _catch_up_once(self) -> None:
        fetch_limit = max(1, int(getattr(self._settings, "log_broker_fetch_limit", 1000)))
        while not self._stop_event.is_set():
            with self._session_factory() as session:
                rows = session.execute(
                    select(ObservabilityStreamEvent)
                    .where(ObservabilityStreamEvent.stream_offset > self._last_seen_offset)
                    .order_by(ObservabilityStreamEvent.stream_offset.asc())
                    .limit(fetch_limit)
                ).scalars().all()
            if not rows:
                return
            self._fan_out(rows)
            self._last_seen_offset = max(self._last_seen_offset, max(int(row.stream_offset) for row in rows))
            if len(rows) < fetch_limit:
                return

    def _fan_out(self, rows: list[ObservabilityStreamEvent]) -> None:
        with self._lock:
            subscribers = list(self._subscribers.items())
        for subscriber_id, subscriber in subscribers:
            if subscriber.disconnected:
                continue
            for row in rows:
                if int(row.stream_offset or 0) <= int(subscriber.min_stream_offset_exclusive):
                    continue
                if not subscriber.match_fn(row):
                    continue
                payload = subscriber.render_fn(row)
                if payload is None:
                    continue
                try:
                    subscriber.queue.put_nowait(payload)
                except Full:
                    subscriber.disconnected = True
                    self.unsubscribe(subscriber_id)
                    break


class ObservabilityStreamRetentionSweeper:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._session_factory = create_session_factory()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="observability-stream-retention", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._started:
            self._thread.join(timeout=2.0)
        self._started = False

    def _run(self) -> None:
        interval = max(15, int(getattr(self._settings, "log_retention_sweep_seconds", 60)))
        while not self._stop_event.is_set():
            try:
                with self._session_factory() as session:
                    prune_observability_stream_events(session=session)
                    session.commit()
            except Exception:
                pass
            self._stop_event.wait(timeout=interval)


def initialize_observability_streaming() -> None:
    settings = get_settings()
    if bool(getattr(settings, "log_bus_enabled", False)):
        get_observability_stream_broker().start()
    get_observability_stream_retention_sweeper().start()


def shutdown_observability_streaming() -> None:
    global _broker, _publisher, _retention_sweeper
    with _broker_lock:
        broker = _broker
        publisher = _publisher
        sweeper = _retention_sweeper
        _broker = None
        _publisher = None
        _retention_sweeper = None
    if broker is not None:
        broker.stop()
    if publisher is not None:
        publisher.stop()
    if sweeper is not None:
        sweeper.stop()
    _reset_redis_client()


def get_observability_stream_broker() -> ObservabilityStreamBroker:
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = ObservabilityStreamBroker()
        return _broker


def get_observability_stream_retention_sweeper() -> ObservabilityStreamRetentionSweeper:
    global _retention_sweeper
    with _broker_lock:
        if _retention_sweeper is None:
            _retention_sweeper = ObservabilityStreamRetentionSweeper()
        return _retention_sweeper


def stream_from_subscriber(*, subscriber: StreamSubscriber) -> Iterator[str]:
    while True:
        try:
            yield subscriber.queue.get(timeout=1.0)
        except Empty:
            if subscriber.disconnected:
                return
            continue


def build_observability_stream_matcher(
    *,
    operation_id: str,
    attempt_id: str | None = None,
) -> Callable[[ObservabilityStreamEvent], bool]:
    normalized_operation_id = str(operation_id or "").strip()
    normalized_attempt_id = str(attempt_id or "").strip() or None

    def _matches(row: ObservabilityStreamEvent) -> bool:
        if str(row.operation_id or "").strip() != normalized_operation_id:
            return False
        if normalized_attempt_id is not None and str(row.attempt_id or "").strip() != normalized_attempt_id:
            return False
        return True

    return _matches


def encode_stream_row(row: ObservabilityStreamEvent) -> str:
    return json.dumps(observability_stream_event_to_payload(row), separators=(",", ":")) + "\n"


def prune_observability_stream_events(
    *,
    session: Session,
    now: datetime | None = None,
) -> int:
    timestamp = now or datetime.now(timezone.utc)
    deleted = 0
    tenant_rows = session.execute(select(Tenant.tenant_id, Tenant.policy_config)).all()
    for tenant_id, raw_policy in tenant_rows:
        policy = normalize_tenant_observability_policy(raw_policy if isinstance(raw_policy, dict) else None)
        cutoff = timestamp - timedelta(days=min(policy.audit_retention_days, 7))
        result = session.execute(
            delete(ObservabilityStreamEvent).where(
                ObservabilityStreamEvent.tenant_id == str(tenant_id or "").strip(),
                ObservabilityStreamEvent.recorded_at < cutoff,
            )
        )
        deleted += int(result.rowcount or 0)
    return deleted
