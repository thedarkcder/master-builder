from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
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
from orchestrator.storage.models import Run

logger = logging.getLogger(__name__)
_ERROR_MARKERS = ("error", "failed", "fatal", "exception", "traceback")
_WORKFLOW_STAGE_PM = "pm"
_WORKFLOW_EXECUTION_STAGES = {"dev", "test", "review"}


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
    reasoning_effort: str | None = None
    issue_description_chars: int | None = None
    codex_session_id: str | None = None


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


def _estimate_token_count(text: str) -> int:
    normalized = str(text or "")
    if not normalized:
        return 0
    return max(1, len(normalized) // 4)


def _is_error_like(message: str) -> bool:
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in _ERROR_MARKERS)


def _collect_context_injection_metrics(*, working_dir: str) -> dict[str, int | bool]:
    repo_root = Path(str(working_dir or ".")).resolve()
    agents_path = repo_root / "AGENTS.md"
    codex_dir = repo_root / ".codex"
    guidance_files = (
        "POLICY.md",
        "ENGINEERING_STANDARDS.md",
        "OPERATING.md",
    )

    agents_chars = 0
    if agents_path.exists():
        try:
            agents_chars = len(agents_path.read_text(encoding="utf-8"))
        except OSError:
            agents_chars = 0

    codex_guidance_chars = 0
    policy_pack_count = 0
    policy_pack_total_chars = 0
    if codex_dir.exists():
        for guidance_file in guidance_files:
            guidance_path = codex_dir / guidance_file
            if not guidance_path.exists():
                continue
            try:
                codex_guidance_chars += len(guidance_path.read_text(encoding="utf-8"))
            except OSError:
                continue
        for policy_pack_path in codex_dir.glob("policy_pack*.json"):
            if not policy_pack_path.is_file():
                continue
            policy_pack_count += 1
            try:
                policy_pack_total_chars += len(policy_pack_path.read_text(encoding="utf-8"))
            except OSError:
                continue

    return {
        "agents_loaded": bool(agents_chars > 0),
        "agents_chars": agents_chars,
        "codex_guidance_chars": codex_guidance_chars,
        "policy_pack_count": policy_pack_count,
        "policy_pack_total_chars": policy_pack_total_chars,
    }


def _emit_invocation_event(
    *,
    context: CodexInvocationContext,
    event_kind: str,
    payload: dict[str, object],
) -> None:
    tenant_id = str(context.tenant_id or "").strip()
    if not tenant_id:
        return
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    event_payload = {
        "event_kind": event_kind,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    try:
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
                stage="telemetry",
                attempt=context.attempt,
                stream="system",
                message=json.dumps(event_payload, sort_keys=True),
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "codex_invocation_telemetry_event_skipped event_kind=%s tenant_id=%s run_id=%s error=%s",
            event_kind,
            tenant_id,
            context.run_id,
            exc,
        )


def _session_column_for_context(*, context: CodexInvocationContext) -> str | None:
    command = str(context.command or "").strip().lower()
    stage = str(context.stage or "").strip().lower()
    if command != "workflow":
        return None
    if stage == _WORKFLOW_STAGE_PM:
        return "pm_session_id"
    if stage in _WORKFLOW_EXECUTION_STAGES:
        return "codex_session_id"
    return None


def _load_run_codex_session_id(*, run_id: str | None, session_column: str | None) -> str | None:
    normalized_run_id = str(run_id or "").strip()
    normalized_column = str(session_column or "").strip()
    if not normalized_run_id or not normalized_column:
        return None
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    try:
        with session_factory() as session:
            run = session.get(Run, normalized_run_id)
            if run is None:
                return None
            candidate = str(getattr(run, normalized_column, "") or "").strip()
            return candidate or None
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "codex_run_session_load_skipped run_id=%s session_column=%s error=%s",
            normalized_run_id,
            normalized_column,
            exc,
        )
        return None


def _persist_run_codex_session_id(*, run_id: str | None, session_id: str | None, session_column: str | None) -> None:
    normalized_run_id = str(run_id or "").strip()
    normalized_session_id = str(session_id or "").strip()
    normalized_column = str(session_column or "").strip()
    if not normalized_run_id or not normalized_session_id or not normalized_column:
        return
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    try:
        with session_factory() as session:
            run = session.get(Run, normalized_run_id)
            if run is None:
                return
            if str(getattr(run, normalized_column, "") or "").strip() == normalized_session_id:
                return
            setattr(run, normalized_column, normalized_session_id)
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "codex_run_session_persist_skipped run_id=%s session_column=%s session_id=%s error=%s",
            normalized_run_id,
            normalized_column,
            normalized_session_id,
            exc,
        )


def _append_raw_log_line(
    *,
    context: CodexInvocationContext,
    stream: str,
    message: str,
) -> None:
    settings = get_settings()
    raw_path = str(getattr(settings, "codex_raw_log_path", "") or "").strip()
    if not raw_path:
        return
    record = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "tenant_id": context.tenant_id,
        "project_id": context.project_id,
        "run_id": context.run_id,
        "issue_key": context.issue_key,
        "invocation_id": context.invocation_id,
        "channel": context.channel,
        "command": context.command,
        "stage": context.stage,
        "attempt": context.attempt,
        "stream": stream,
        "message": message,
    }
    try:
        target = Path(raw_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        logger.exception("codex_raw_log_write_failed path=%s", raw_path)


def _should_persist_db_line(*, stream: str, message: str, line_index: int, sample_every: int) -> bool:
    if stream == "system":
        return True
    if _is_error_like(message):
        return True
    if line_index <= 20:
        return True
    if sample_every <= 0:
        return False
    return line_index % sample_every == 0


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
    prompt_metrics = {
        "system_prompt_chars": len(system_prompt),
        "user_prompt_chars": len(user_prompt),
        "estimated_prompt_tokens": _estimate_token_count(system_prompt) + _estimate_token_count(user_prompt),
        "issue_description_chars": max(0, int(context.issue_description_chars or 0)),
    }
    context_metrics = _collect_context_injection_metrics(working_dir=context.working_dir)
    session_column = _session_column_for_context(context=context)
    explicit_session_id = str(context.codex_session_id or "").strip() or None
    run_session_id = _load_run_codex_session_id(run_id=context.run_id, session_column=session_column)
    resume_session_id = explicit_session_id or run_session_id
    invocation_started_monotonic = time.monotonic()
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
        reasoning_effort=context.reasoning_effort,
        issue_description_chars=context.issue_description_chars,
        codex_session_id=resume_session_id,
    )
    sink_state = {
        "turn_context_events": 0,
        "no_assistant_output_detected": False,
        "db_persisted_lines": 0,
        "raw_lines_written": 0,
        "codex_session_id": resume_session_id or "",
    }
    _emit_invocation_event(
        context=invocation_context,
        event_kind="stage_invocation_started",
        payload={
            "status": "started",
            "resumed_session": bool(resume_session_id),
            "codex_session_id": resume_session_id or "",
            **prompt_metrics,
            **context_metrics,
        },
    )
    payload: dict | None = None
    failure_reason: str | None = None
    try:
        payload = runtime.run_json(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            working_dir=context.working_dir,
            on_log_line=_combined_log_sink(
                context=invocation_context,
                extra_on_log_line=extra_on_log_line,
                sink_state=sink_state,
            ),
            reasoning_effort=context.reasoning_effort,
            resume_session_id=resume_session_id,
            on_session_id=lambda session_id: _capture_session_id(
                context=invocation_context,
                sink_state=sink_state,
                session_id=session_id,
                session_column=session_column,
            ),
        )
        return payload
    except Exception as exc:  # noqa: BLE001
        failure_reason = str(exc)
        if "empty response" in str(exc).lower():
            sink_state["no_assistant_output_detected"] = True
        raise
    finally:
        _get_log_writer().flush_invocation(invocation_id=invocation_id)
        duration_ms = int((time.monotonic() - invocation_started_monotonic) * 1000)
        _emit_invocation_event(
            context=invocation_context,
            event_kind="stage_invocation_finished",
            payload={
                "status": "succeeded" if failure_reason is None else "failed",
                "duration_ms": duration_ms,
                "turn_context_events": int(sink_state["turn_context_events"]),
                "no_assistant_output_detected": bool(sink_state["no_assistant_output_detected"]),
                "db_persisted_lines": int(sink_state["db_persisted_lines"]),
                "raw_lines_written": int(sink_state["raw_lines_written"]),
                "resumed_session": bool(resume_session_id),
                "codex_session_id": str(sink_state.get("codex_session_id") or ""),
                "output_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
                "error": failure_reason or "",
                **prompt_metrics,
                **context_metrics,
            },
        )
        if bool(sink_state["no_assistant_output_detected"]):
            _emit_invocation_event(
                context=invocation_context,
                event_kind="no_assistant_output_event",
                payload={
                    "status": "observed",
                    "duration_ms": duration_ms,
                },
            )


def _capture_session_id(
    *,
    context: CodexInvocationContext,
    sink_state: dict[str, int | bool | str],
    session_id: str,
    session_column: str | None,
) -> None:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return
    sink_state["codex_session_id"] = normalized_session_id
    _persist_run_codex_session_id(
        run_id=context.run_id,
        session_id=normalized_session_id,
        session_column=session_column,
    )


def _combined_log_sink(
    *,
    context: CodexInvocationContext,
    extra_on_log_line: Callable[[str, str], None] | None,
    sink_state: dict[str, int | bool],
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
        message_text = str(message or "")
        line_counter = int(sink_state.get("raw_lines_written", 0)) + 1
        sink_state["raw_lines_written"] = line_counter
        if "turn_context" in message_text:
            sink_state["turn_context_events"] = int(sink_state.get("turn_context_events", 0)) + 1
        if (
            "task_complete" in message_text
            and "last_agent_message" in message_text
            and "null" in message_text
        ):
            sink_state["no_assistant_output_detected"] = True
        _append_raw_log_line(context=context, stream=stream, message=message_text)
        settings = get_settings()
        sample_every = max(1, int(getattr(settings, "codex_db_log_sampling_interval", 100)))
        if not _should_persist_db_line(
            stream=stream,
            message=message_text,
            line_index=line_counter,
            sample_every=sample_every,
        ):
            if extra_on_log_line is not None:
                extra_on_log_line(stream, message)
            return
        try:
            _enqueue_codex_log_line(context=context, stream=stream, message=message_text)
            sink_state["db_persisted_lines"] = int(sink_state.get("db_persisted_lines", 0)) + 1
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
