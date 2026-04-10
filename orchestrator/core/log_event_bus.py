from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
import json
import logging
from queue import Empty, Full, Queue
import threading

from sqlalchemy import Select, desc, func, select
from sqlalchemy.event import listens_for
from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.platform_metrics import platform_metrics
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import RunStreamEvent

logger = logging.getLogger(__name__)

RUN_STREAM_REDIS_CHANNEL = "run_stream_events"
EVENT_KIND_CODEX_LOG = "codex_log"
EVENT_KIND_AGENT_LIFECYCLE = "agent_lifecycle"
_SESSION_OFFSETS_KEY = "_run_stream_offsets"

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
    run_id: str | None


@dataclass
class StreamSubscriber:
    queue: Queue[str]
    match_fn: Callable[[RunStreamEvent], bool]
    render_fn: Callable[[RunStreamEvent], str | None]
    min_stream_offset_exclusive: int = 0
    disconnected: bool = False


_redis_client_lock = threading.Lock()
_redis_client: Redis | None = None
_broker_lock = threading.Lock()
_broker: RunStreamBroker | None = None
_retention_sweeper: RunStreamRetentionSweeper | None = None
_publisher_lock = threading.Lock()
_publisher: RunStreamPublisher | None = None


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
        except Exception:  # noqa: BLE001
            logger.debug("run_stream_redis_close_failed", exc_info=True)


def register_stream_offsets(*, session: Session, rows: list[RunStreamEvent]) -> None:
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
            run_id=rows[0].run_id if rows else None,
        )
    )


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
            "run_id": message.run_id,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    try:
        client.publish(RUN_STREAM_REDIS_CHANNEL, payload)
    except RedisError as exc:
        logger.warning("run_stream_redis_publish_failed error=%s", exc)
        _reset_redis_client()


def _redis_wakeups_enabled() -> bool:
    settings = get_settings()
    redis_url = str(getattr(settings, "redis_url", "") or "").strip()
    return bool(getattr(settings, "log_bus_enabled", False) and redis_url and Redis is not None)


class RunStreamPublisher:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._stop_event = threading.Event()
        self._started = False
        max_queue_size = max(64, int(getattr(self._settings, "log_db_batch_size", 250)) * 4)
        self._queue: Queue[StreamWakeup] = Queue(maxsize=max_queue_size)
        self._thread = threading.Thread(target=self._run, name="run-stream-publisher", daemon=True)

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
            logger.warning(
                "run_stream_publish_queue_full min_offset=%s max_offset=%s count=%s",
                message.min_offset,
                message.max_offset,
                message.count,
            )

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                message = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                _publish_wakeup(message)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "run_stream_publish_worker_failed min_offset=%s max_offset=%s",
                    message.min_offset,
                    message.max_offset,
                )
            finally:
                self._queue.task_done()


def get_run_stream_publisher() -> RunStreamPublisher:
    global _publisher
    with _publisher_lock:
        if _publisher is None:
            _publisher = RunStreamPublisher()
        return _publisher


def publish_stream_wakeups(messages: list[StreamWakeup]) -> None:
    if not messages or not _redis_wakeups_enabled():
        return
    publisher = get_run_stream_publisher()
    for message in messages:
        if isinstance(message, StreamWakeup):
            publisher.enqueue(message)


@listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    messages = list(session.info.pop(_SESSION_OFFSETS_KEY, []))
    publish_stream_wakeups(messages)


@listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_SESSION_OFFSETS_KEY, None)


def _encode_run_stream_row(row: RunStreamEvent) -> str | None:
    if row.event_kind == EVENT_KIND_AGENT_LIFECYCLE:
        if not row.event_type or not row.run_id or not row.agent_id:
            return None
        payload = {
            "event_type": row.event_type,
            "run_id": row.run_id,
            "issue_key": row.issue_key,
            "project_id": row.project_id,
            "agent_id": row.agent_id,
            "recorded_at": row.recorded_at.isoformat(),
        }
        return json.dumps(payload, separators=(",", ":")) + "\n"
    if row.event_kind == EVENT_KIND_CODEX_LOG:
        if not row.agent_id or not row.stage or not row.stream:
            return None
        payload = {
            "event_kind": EVENT_KIND_CODEX_LOG,
            "invocation_id": row.invocation_id,
            "channel": row.channel,
            "command": row.command,
            "working_dir": row.working_dir,
            "run_id": row.run_id,
            "issue_key": row.issue_key,
            "project_id": row.project_id,
            "agent_id": row.agent_id,
            "stage": row.stage,
            "attempt": row.attempt,
            "stream": row.stream,
            "message": row.message,
            "recorded_at": row.recorded_at.isoformat(),
        }
        return json.dumps(payload, separators=(",", ":")) + "\n"
    return None


def build_run_stream_snapshot_query(*, run_id: str, initial_event_limit: int, initial_log_limit: int) -> list[RunStreamEvent]:
    session_factory = create_session_factory()
    with session_factory() as session:
        lifecycle_rows = session.execute(
            select(RunStreamEvent)
            .where(RunStreamEvent.run_id == run_id, RunStreamEvent.event_kind == EVENT_KIND_AGENT_LIFECYCLE)
            .order_by(desc(RunStreamEvent.stream_offset))
            .limit(max(1, initial_event_limit))
        ).scalars().all()
        log_rows = session.execute(
            select(RunStreamEvent)
            .where(RunStreamEvent.run_id == run_id, RunStreamEvent.event_kind == EVENT_KIND_CODEX_LOG)
            .order_by(desc(RunStreamEvent.stream_offset))
            .limit(max(1, initial_log_limit))
        ).scalars().all()
    ordered = sorted([*lifecycle_rows, *log_rows], key=lambda row: int(row.stream_offset))
    return ordered


def build_codex_stream_snapshot_query(
    *,
    tenant_id: str | None,
    project_id: str | None,
    run_id: str | None,
    channel: str | None,
    command: str | None,
    limit: int,
) -> list[RunStreamEvent]:
    session_factory = create_session_factory()
    query: Select[tuple[RunStreamEvent]] = (
        select(RunStreamEvent)
        .where(RunStreamEvent.event_kind == EVENT_KIND_CODEX_LOG)
        .order_by(desc(RunStreamEvent.stream_offset))
        .limit(max(1, limit))
    )
    if tenant_id:
        query = query.where(RunStreamEvent.tenant_id == tenant_id)
    if project_id:
        query = query.where(RunStreamEvent.project_id == project_id)
    if run_id:
        query = query.where(RunStreamEvent.run_id == run_id)
    if channel:
        query = query.where(RunStreamEvent.channel == channel)
    if command:
        query = query.where(RunStreamEvent.command == command)
    with session_factory() as session:
        rows = session.execute(query).scalars().all()
    return list(reversed(rows))


class RunStreamBroker:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._session_factory = create_session_factory()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._subscribers: dict[str, StreamSubscriber] = {}
        self._last_seen_offset = 0
        self._started = False
        self._redis_connected = False
        self._poll_thread = threading.Thread(target=self._run_poll_loop, name="run-stream-broker", daemon=True)
        self._redis_thread = threading.Thread(target=self._run_redis_loop, name="run-stream-redis", daemon=True)

    def start(self) -> None:
        if self._started:
            return
        self._last_seen_offset = self._load_high_water_mark()
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
        platform_metrics.record_log_broker_state(
            last_seen_offset=self._last_seen_offset,
            subscriber_count=0,
            redis_connected=False,
        )

    def subscribe(
        self,
        *,
        subscriber_id: str,
        buffer_size: int,
        match_fn: Callable[[RunStreamEvent], bool],
        render_fn: Callable[[RunStreamEvent], str | None],
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
            count = len(self._subscribers)
        platform_metrics.record_log_broker_state(
            last_seen_offset=self._last_seen_offset,
            subscriber_count=count,
            redis_connected=self._redis_connected,
        )
        return subscriber

    def unsubscribe(self, subscriber_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscriber_id, None)
            count = len(self._subscribers)
        platform_metrics.record_log_broker_state(
            last_seen_offset=self._last_seen_offset,
            subscriber_count=count,
            redis_connected=self._redis_connected,
        )

    def request_catchup(self) -> None:
        self._wake_event.set()

    def _load_high_water_mark(self) -> int:
        with self._session_factory() as session:
            value = session.execute(select(func.max(RunStreamEvent.stream_offset))).scalar_one_or_none()
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
                pubsub.subscribe(RUN_STREAM_REDIS_CHANNEL)
                self._redis_connected = True
                platform_metrics.record_log_broker_state(
                    last_seen_offset=self._last_seen_offset,
                    subscriber_count=self._subscriber_count(),
                    redis_connected=True,
                )
                while not self._stop_event.is_set():
                    message = pubsub.get_message(timeout=timeout_seconds)
                    if message is None:
                        continue
                    self.request_catchup()
            except RedisError as exc:
                logger.warning("run_stream_redis_listener_failed error=%s", exc)
            finally:
                self._redis_connected = False
                platform_metrics.record_log_broker_state(
                    last_seen_offset=self._last_seen_offset,
                    subscriber_count=self._subscriber_count(),
                    redis_connected=False,
                )
                try:
                    pubsub.close()
                except Exception:  # noqa: BLE001
                    logger.debug("run_stream_redis_pubsub_close_failed", exc_info=True)
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    logger.debug("run_stream_redis_client_close_failed", exc_info=True)
                self._stop_event.wait(timeout=1.0)

    def _subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def _catch_up_once(self) -> None:
        fetch_limit = max(1, int(getattr(self._settings, "log_broker_fetch_limit", 1000)))
        while not self._stop_event.is_set():
            with self._session_factory() as session:
                rows = session.execute(
                    select(RunStreamEvent)
                    .where(RunStreamEvent.stream_offset > self._last_seen_offset)
                    .order_by(RunStreamEvent.stream_offset.asc())
                    .limit(fetch_limit)
                ).scalars().all()
            if not rows:
                platform_metrics.record_log_broker_state(
                    last_seen_offset=self._last_seen_offset,
                    subscriber_count=self._subscriber_count(),
                    redis_connected=self._redis_connected,
                )
                return
            self._fan_out(rows)
            self._last_seen_offset = max(self._last_seen_offset, max(int(row.stream_offset) for row in rows))
            if len(rows) < fetch_limit:
                platform_metrics.record_log_broker_state(
                    last_seen_offset=self._last_seen_offset,
                    subscriber_count=self._subscriber_count(),
                    redis_connected=self._redis_connected,
                )
                return

    def _fan_out(self, rows: list[RunStreamEvent]) -> None:
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


class RunStreamRetentionSweeper:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._session_factory = create_session_factory()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, name="run-stream-retention", daemon=True)
        self._started = False

    def start(self) -> None:
        if self._started or not bool(getattr(self._settings, "log_bus_enabled", False)):
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
                from orchestrator.core.agent_observability import prune_agent_lifecycle_events
                from orchestrator.core.run_logs import prune_run_log_events, prune_run_stream_events

                with self._session_factory() as session:
                    prune_agent_lifecycle_events(session=session)
                    prune_run_log_events(session=session)
                    prune_run_stream_events(session=session)
                    session.commit()
            except Exception:  # noqa: BLE001
                logger.exception("run_stream_retention_sweep_failed")
            self._stop_event.wait(timeout=interval)


def initialize_run_streaming() -> None:
    settings = get_settings()
    if not bool(getattr(settings, "log_bus_enabled", False)):
        return
    get_run_stream_broker().start()
    get_run_stream_retention_sweeper().start()


def shutdown_run_streaming() -> None:
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


def get_run_stream_broker() -> RunStreamBroker:
    global _broker
    with _broker_lock:
        if _broker is None:
            _broker = RunStreamBroker()
        return _broker


def get_run_stream_retention_sweeper() -> RunStreamRetentionSweeper:
    global _retention_sweeper
    with _broker_lock:
        if _retention_sweeper is None:
            _retention_sweeper = RunStreamRetentionSweeper()
        return _retention_sweeper


def stream_from_subscriber(*, subscriber: StreamSubscriber) -> Iterator[str]:
    while True:
        try:
            yield subscriber.queue.get(timeout=1.0)
        except Empty:
            if subscriber.disconnected:
                return
            continue


def build_run_stream_matcher(*, run_id: str) -> Callable[[RunStreamEvent], bool]:
    return lambda row: str(row.run_id or "") == run_id


def build_codex_stream_matcher(
    *,
    tenant_id: str | None,
    project_id: str | None,
    run_id: str | None,
    channel: str | None,
    command: str | None,
) -> Callable[[RunStreamEvent], bool]:
    def _matches(row: RunStreamEvent) -> bool:
        if row.event_kind != EVENT_KIND_CODEX_LOG:
            return False
        if tenant_id and row.tenant_id != tenant_id:
            return False
        if project_id and row.project_id != project_id:
            return False
        if run_id and row.run_id != run_id:
            return False
        if channel and row.channel != channel:
            return False
        if command and row.command != command:
            return False
        return True

    return _matches


def encode_stream_row(row: RunStreamEvent) -> str | None:
    return _encode_run_stream_row(row)
