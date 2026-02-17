from __future__ import annotations

from dataclasses import dataclass
import logging
from queue import Empty, Full, Queue
import threading
import time
from typing import Callable
from uuid import uuid4

from orchestrator.core.config import get_settings
from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.codex_telemetry import build_codex_log_sink
from orchestrator.core.run_logs import record_run_log_event, record_run_log_events_batch
from orchestrator.storage.db import create_session_factory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodexInvocationContext:
    channel: str
    tenant_id: str | None
    project_id: str | None
    command: str
    stage: str
    working_dir: str
    issue_key: str | None = None
    run_id: str | None = None
    attempt: int | None = None
    invocation_id: str | None = None


@dataclass(frozen=True)
class _QueuedLogLine:
    context: CodexInvocationContext
    stream: str
    message: str


class _AsyncCodexLogWriter:
    def __init__(self, *, max_queue_size: int = 2000) -> None:
        self._queue: Queue[_QueuedLogLine] = Queue(maxsize=max_queue_size)
        self._pending_counts: dict[str, int] = {}
        self._pending_lock = threading.Lock()
        self._pending_cond = threading.Condition(self._pending_lock)
        self._worker = threading.Thread(target=self._run, daemon=True, name="codex-log-writer")
        self._worker.start()
        self._dropped = 0
        settings = get_settings()
        self._batch_size = max(1, int(getattr(settings, "codex_log_batch_size", 50)))
        self._batch_flush_ms = max(1, int(getattr(settings, "codex_log_batch_flush_ms", 50)))

    def enqueue(self, *, context: CodexInvocationContext, stream: str, message: str) -> bool:
        invocation_id = str(context.invocation_id or "").strip()
        if not invocation_id:
            return False
        item = _QueuedLogLine(context=context, stream=stream, message=message)
        with self._pending_cond:
            self._pending_counts[invocation_id] = self._pending_counts.get(invocation_id, 0) + 1
        try:
            self._queue.put_nowait(item)
        except Full:
            with self._pending_cond:
                current = self._pending_counts.get(invocation_id, 0)
                if current <= 1:
                    self._pending_counts.pop(invocation_id, None)
                else:
                    self._pending_counts[invocation_id] = current - 1
                self._pending_cond.notify_all()
            self._dropped += 1
            if self._dropped % 100 == 1:
                logger.warning("codex_log_queue_full dropped=%s", self._dropped)
            return False
        return True

    def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> None:
        normalized_invocation_id = str(invocation_id or "").strip()
        if not normalized_invocation_id:
            return
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._pending_cond:
            while self._pending_counts.get(normalized_invocation_id, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "codex_log_flush_timeout invocation_id=%s pending=%s",
                        normalized_invocation_id,
                        self._pending_counts.get(normalized_invocation_id, 0),
                    )
                    return
                self._pending_cond.wait(timeout=remaining)

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.2)
            except Empty:
                continue
            items = [item]
            flush_deadline = time.monotonic() + (self._batch_flush_ms / 1000.0)
            while len(items) < self._batch_size:
                timeout = max(0.0, flush_deadline - time.monotonic())
                if timeout <= 0:
                    break
                try:
                    items.append(self._queue.get(timeout=timeout))
                except Empty:
                    break
            invocation_ids = {
                str(queued.context.invocation_id or "").strip()
                for queued in items
                if str(queued.context.invocation_id or "").strip()
            }
            try:
                _persist_codex_log_lines(items=items)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "codex_log_persist_failed batch_size=%s invocation_count=%s error=%s",
                    len(items),
                    len(invocation_ids),
                    exc,
                )
            finally:
                with self._pending_cond:
                    for invocation_id in invocation_ids:
                        current = self._pending_counts.get(invocation_id, 0)
                        decremented = sum(
                            1
                            for queued in items
                            if str(queued.context.invocation_id or "").strip() == invocation_id
                        )
                        remaining = current - decremented
                        if remaining <= 0:
                            self._pending_counts.pop(invocation_id, None)
                        else:
                            self._pending_counts[invocation_id] = remaining
                    self._pending_cond.notify_all()
                for _ in items:
                    self._queue.task_done()


_log_writer: _AsyncCodexLogWriter | None = None


def _get_log_writer() -> _AsyncCodexLogWriter:
    global _log_writer
    if _log_writer is None:
        _log_writer = _AsyncCodexLogWriter()
    return _log_writer


def _enqueue_codex_log_line(*, context: CodexInvocationContext, stream: str, message: str) -> None:
    writer = _get_log_writer()
    writer.enqueue(context=context, stream=stream, message=message)


def invoke_codex_json(
    *,
    runtime: CodexRuntime,
    context: CodexInvocationContext,
    system_prompt: str,
    user_prompt: str,
    extra_on_log_line: Callable[[str, str], None] | None = None,
) -> dict:
    invocation_id = context.invocation_id or uuid4().hex
    invocation_context = CodexInvocationContext(
        channel=context.channel,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        command=context.command,
        stage=context.stage,
        working_dir=context.working_dir,
        issue_key=context.issue_key,
        run_id=context.run_id,
        attempt=context.attempt,
        invocation_id=invocation_id,
    )
    try:
        return runtime.run_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            working_dir=context.working_dir,
            on_log_line=_combined_log_sink(
                context=invocation_context,
                extra_on_log_line=extra_on_log_line,
            ),
        )
    finally:
        _get_log_writer().flush_invocation(invocation_id=invocation_id)


def _combined_log_sink(
    *,
    context: CodexInvocationContext,
    extra_on_log_line: Callable[[str, str], None] | None,
) -> Callable[[str, str], None]:
    telemetry_sink = build_codex_log_sink(
        channel=context.channel,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        command=f"{context.command}.{context.stage}",
        issue_key=context.issue_key,
        working_dir=context.working_dir,
    )

    def _sink(stream: str, message: str) -> None:
        telemetry_sink(stream, message)
        try:
            _enqueue_codex_log_line(context=context, stream=stream, message=message)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "codex_log_persist_failed tenant_id=%s run_id=%s channel=%s command=%s stage=%s error=%s",
                context.tenant_id,
                context.run_id,
                context.channel,
                context.command,
                context.stage,
                exc,
            )
        if extra_on_log_line is not None:
            extra_on_log_line(stream, message)

    return _sink


def _persist_codex_log_line(*, context: CodexInvocationContext, stream: str, message: str) -> None:
    tenant_id = str(context.tenant_id or "").strip()
    if not tenant_id:
        return
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    with session_factory() as session:
        record_run_log_event(
            session=session,
            tenant_id=tenant_id,
            project_id=context.project_id,
            run_id=context.run_id,
            issue_key=context.issue_key,
            agent_id=settings.agent_id,
            invocation_id=str(context.invocation_id or "").strip(),
            channel=context.channel,
            command=f"{context.command}.{context.stage}",
            working_dir=context.working_dir,
            stage=context.stage,
            attempt=context.attempt,
            stream=stream,
            message=message,
        )
        session.commit()


def _persist_codex_log_lines(*, items: list[_QueuedLogLine]) -> None:
    if not items:
        return
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    with session_factory() as session:
        batched_events: list[dict[str, object]] = []
        for item in items:
            tenant_id = str(item.context.tenant_id or "").strip()
            if not tenant_id:
                continue
            batched_events.append(
                {
                    "tenant_id": tenant_id,
                    "project_id": item.context.project_id,
                    "run_id": item.context.run_id,
                    "issue_key": item.context.issue_key,
                    "agent_id": settings.agent_id,
                    "invocation_id": str(item.context.invocation_id or "").strip(),
                    "channel": item.context.channel,
                    "command": f"{item.context.command}.{item.context.stage}",
                    "working_dir": item.context.working_dir,
                    "stage": item.context.stage,
                    "attempt": item.context.attempt,
                    "stream": item.stream,
                    "message": item.message,
                }
            )
        if not batched_events:
            return
        if len(batched_events) == 1:
            single = batched_events[0]
            record_run_log_event(
                session=session,
                tenant_id=str(single["tenant_id"]),
                project_id=single["project_id"] if isinstance(single["project_id"], str) else None,
                run_id=single["run_id"] if isinstance(single["run_id"], str) else None,
                issue_key=single["issue_key"] if isinstance(single["issue_key"], str) else None,
                agent_id=str(single["agent_id"]),
                invocation_id=single["invocation_id"] if isinstance(single["invocation_id"], str) else None,
                channel=single["channel"] if isinstance(single["channel"], str) else None,
                command=single["command"] if isinstance(single["command"], str) else None,
                working_dir=single["working_dir"] if isinstance(single["working_dir"], str) else None,
                stage=str(single["stage"]),
                attempt=single["attempt"] if isinstance(single["attempt"], int) else None,
                stream=str(single["stream"]),
                message=str(single["message"]),
            )
        else:
            record_run_log_events_batch(
                session=session,
                events=batched_events,
            )
        session.commit()
