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

from sqlalchemy.orm import Session

from orchestrator.core.runtime.models import normalize_codex_reasoning_effort
from orchestrator.core.observability.audit import record_audit_event
from orchestrator.core.config import get_settings
from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.knowledge.base import KnowledgeEmbeddingAccessMode, build_knowledge_prompt_context
from orchestrator.core.projects.policy import resolve_effective_policy
from orchestrator.core.runtime.runtime import CodexRuntime, CodexRuntimeError, normalize_runtime_token_usage
from orchestrator.core.observability.telemetry import build_runtime_log_sink
from orchestrator.core.observability.logging_pane import emit_logging_pane_event, emit_logging_pane_events_batch
from orchestrator.core.observability.logging_pane import extract_turn_completed_usage
from orchestrator.core.observability.observability_stream import record_observability_stream_event
from orchestrator.core.workflow.checkpoints import checkpoint_kind_for_stage, normalize_checkpoint_stage, upsert_workflow_checkpoint
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Run, Tenant

logger = logging.getLogger(__name__)
_LIVE_INVOCATION_LOGGER = logging.getLogger("orchestrator.runtime_invocation")
_WORKFLOW_STAGE_PM = "pm"
_WORKFLOW_EXECUTION_STAGES = {"dev", "test", "review"}
_JSON_PARSE_ERROR_MARKERS = (
    "not include json",
    "invalid json payload",
    "did not return a json object",
    "expecting value",
    "empty response",
)
_TOOL_REQUEST_TYPE = "tool_request"
_FINAL_RESPONSE_TYPE = "final_response"


@dataclass(frozen=True)
class AgentInvocationContext:
    channel: str
    tenant_id: str | None
    project_id: str | None
    command: str
    stage: str
    working_dir: str
    workflow_id: str | None = None
    operation_id: str | None = None
    attempt_id: str | None = None
    issue_key: str | None = None
    run_id: str | None = None
    attempt: int | None = None
    invocation_id: str | None = None
    reasoning_effort: str | None = None
    issue_description_chars: int | None = None
    codex_session_id: str | None = None
    worker_platform: str | None = None
    db_session: Session | None = None


class RuntimeInvocationError(RuntimeError):
    """Base error for runtime invocation contract failures."""


class RuntimeJsonContractError(RuntimeInvocationError):
    """Raised when a runtime JSON invocation cannot satisfy the JSON contract."""


class ToolBridgeProtocolError(RuntimeInvocationError):
    """Raised when the runtime violates the governed tool bridge protocol."""


class ToolBridgeExhaustedError(RuntimeInvocationError):
    """Raised when the governed tool bridge cannot reach a final response."""


class NativeToolPolicyError(RuntimeInvocationError):
    """Raised when a runtime uses a native tool outside the stage allowlist."""


@dataclass(frozen=True)
class _QueuedLogLine:
    context: AgentInvocationContext
    stream: str
    message: str


@dataclass(frozen=True)
class _RuntimeLogFlushResult:
    invocation_id: str
    completed: bool
    pending_count: int = 0
    failure_message: str | None = None


class _AsyncRuntimeLogWriter:
    def __init__(self, *, max_queue_size: int = 2000) -> None:
        self._queue: Queue[_QueuedLogLine] = Queue(maxsize=max_queue_size)
        self._pending_counts: dict[str, int] = {}
        self._pending_failures: dict[str, str] = {}
        self._pending_lock = threading.Lock()
        self._pending_cond = threading.Condition(self._pending_lock)
        self._worker = threading.Thread(target=self._run, daemon=True, name="runtime-log-writer")
        self._worker.start()
        settings = get_settings()
        self._batch_size = max(
            1,
            int(
                getattr(
                    settings,
                    "log_db_batch_size",
                    getattr(settings, "runtime_log_batch_size", 50),
                )
            ),
        )
        self._batch_flush_ms = max(
            1,
            int(
                getattr(
                    settings,
                    "log_db_batch_flush_ms",
                    getattr(settings, "runtime_log_batch_flush_ms", 50),
                )
            ),
        )

    def enqueue(self, *, context: AgentInvocationContext, stream: str, message: str) -> bool:
        invocation_id = str(context.invocation_id or "").strip()
        if not invocation_id:
            raise ValueError("Runtime log persistence requires invocation_id")
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
                self._pending_failures[invocation_id] = "Runtime log persistence queue is full"
            logger.warning("runtime_log_enqueue_dropped invocation_id=%s reason=queue_full", invocation_id)
            return False
        return True

    def flush_invocation(self, *, invocation_id: str, timeout_seconds: float = 3.0) -> _RuntimeLogFlushResult:
        normalized_invocation_id = str(invocation_id or "").strip()
        if not normalized_invocation_id:
            return _RuntimeLogFlushResult(invocation_id="", completed=True)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._pending_cond:
            while self._pending_counts.get(normalized_invocation_id, 0) > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    pending = self._pending_counts.get(normalized_invocation_id, 0)
                    failure_message = (
                        f"Runtime log persistence flush timed out for invocation_id={normalized_invocation_id} "
                        f"pending={pending}"
                    )
                    logger.warning("runtime_log_flush_incomplete %s", failure_message)
                    return _RuntimeLogFlushResult(
                        invocation_id=normalized_invocation_id,
                        completed=False,
                        pending_count=pending,
                        failure_message=failure_message,
                    )
                self._pending_cond.wait(timeout=remaining)
            failure = self._pending_failures.pop(normalized_invocation_id, None)
            if failure is not None:
                failure_message = (
                    f"Runtime log persistence failed for invocation_id={normalized_invocation_id}: {failure}"
                )
                logger.warning("runtime_log_flush_failed %s", failure_message)
                return _RuntimeLogFlushResult(
                    invocation_id=normalized_invocation_id,
                    completed=True,
                    failure_message=failure_message,
                )
            return _RuntimeLogFlushResult(invocation_id=normalized_invocation_id, completed=True)

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
            grouped_items: dict[str, list[_QueuedLogLine]] = {invocation_id: [] for invocation_id in invocation_ids}
            for queued in items:
                invocation_id = str(queued.context.invocation_id or "").strip()
                if invocation_id:
                    grouped_items.setdefault(invocation_id, []).append(queued)
            try:
                for invocation_id, group in grouped_items.items():
                    try:
                        _persist_runtime_log_lines(items=group)
                    except Exception as exc:  # noqa: BLE001
                        failure_message = str(exc)
                        logger.exception(
                            "runtime_log_persist_failed invocation_id=%s batch_size=%s error=%s",
                            invocation_id,
                            len(group),
                            exc,
                        )
                        with self._pending_cond:
                            self._pending_failures[invocation_id] = failure_message
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


_log_writer: _AsyncRuntimeLogWriter | None = None


def _get_log_writer() -> _AsyncRuntimeLogWriter:
    global _log_writer
    if _log_writer is None:
        _log_writer = _AsyncRuntimeLogWriter()
    return _log_writer


def _estimate_token_count(text: str) -> int:
    normalized = str(text or "")
    if not normalized:
        return 0
    return max(1, len(normalized) // 4)


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


def _resolve_knowledge_policy_for_context(
    *,
    context: AgentInvocationContext,
) -> tuple[str | None, bool, str, str | None, str, bool]:
    settings = get_settings()
    database_url = str(getattr(settings, "database_url", "") or "").strip()
    default_reasoning_effort = str(getattr(settings, "codex_reasoning_effort", "medium") or "medium")
    tenant_id = str(context.tenant_id or "").strip()
    if not tenant_id:
        return (
            context.project_id,
            bool(getattr(settings, "knowledge_base_enabled_default", True)),
            str(getattr(settings, "knowledge_auto_answer_mode_default", "aggressive")),
            str(getattr(settings, "codex_model", "") or "").strip() or None,
            default_reasoning_effort,
            False,
        )
    if not database_url:
        return (
            context.project_id,
            bool(getattr(settings, "knowledge_base_enabled_default", True)),
            str(getattr(settings, "knowledge_auto_answer_mode_default", "aggressive")),
            str(getattr(settings, "codex_model", "") or "").strip() or None,
            default_reasoning_effort,
            False,
        )

    session_factory = create_session_factory(database_url=database_url)
    resolved_project_id = context.project_id
    knowledge_enabled = bool(getattr(settings, "knowledge_base_enabled_default", True))
    knowledge_mode = str(getattr(settings, "knowledge_auto_answer_mode_default", "aggressive"))
    codex_model_override = str(getattr(settings, "codex_model", "") or "").strip() or None
    codex_reasoning_effort = default_reasoning_effort
    has_explicit_reasoning_override = False
    try:
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            if tenant is None:
                return (
                    resolved_project_id,
                    knowledge_enabled,
                    knowledge_mode,
                    codex_model_override,
                    codex_reasoning_effort,
                    has_explicit_reasoning_override,
                )
            tenant_policy = tenant.policy_config or {}
            if resolved_project_id is None and context.run_id:
                run = session.get(Run, context.run_id)
                if run is not None and run.tenant_id == tenant_id:
                    resolved_project_id = run.project_id
            project_overrides: dict[str, object] = {}
            if resolved_project_id:
                project = session.get(Project, resolved_project_id)
                if project is not None and project.tenant_id == tenant_id:
                    project_overrides = project.policy_overrides or {}
            has_explicit_reasoning_override = (
                normalize_codex_reasoning_effort(project_overrides.get("codex_reasoning_effort")) is not None
                or normalize_codex_reasoning_effort(tenant_policy.get("codex_reasoning_effort")) is not None
            )
            effective = resolve_effective_policy(
                tenant_policy=tenant_policy,
                project_overrides=project_overrides,
                default_codex_model=None,
                default_codex_reasoning_effort=default_reasoning_effort,
            )
            knowledge_enabled = bool(effective.get("knowledge_base_enabled", knowledge_enabled))
            normalized_mode = str(effective.get("knowledge_auto_answer_mode") or "").strip().lower()
            if normalized_mode in {"safe", "balanced", "aggressive"}:
                knowledge_mode = normalized_mode
            normalized_model = str(effective.get("codex_model") or "").strip()
            if normalized_model:
                codex_model_override = normalized_model
            codex_reasoning_effort = (
                str(effective.get("codex_reasoning_effort") or codex_reasoning_effort).strip().lower()
                or codex_reasoning_effort
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "knowledge_policy_resolution_skipped tenant_id=%s run_id=%s error=%s",
            tenant_id,
            context.run_id,
            exc,
        )
    return (
        resolved_project_id,
        knowledge_enabled,
        knowledge_mode,
        codex_model_override,
        codex_reasoning_effort,
        has_explicit_reasoning_override,
    )


def _augment_prompt_with_knowledge_context(
    *,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict[str, object], str | None, str, bool]:
    settings = get_settings()
    default_codex_reasoning_effort = str(getattr(settings, "codex_reasoning_effort", "medium") or "medium")
    database_url = str(getattr(settings, "database_url", "") or "").strip()
    tenant_id = str(context.tenant_id or "").strip()
    resolved_codex_reasoning_effort = default_codex_reasoning_effort
    resolved_model_override = str(getattr(settings, "codex_model", "") or "").strip() or None
    has_explicit_reasoning_override = False
    if tenant_id:
        (
            _,
            _,
            _,
            resolved_model_override,
            resolved_codex_reasoning_effort,
            has_explicit_reasoning_override,
        ) = _resolve_knowledge_policy_for_context(
            context=context
        )
    if not bool(getattr(settings, "knowledge_injection_enabled", True)):
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            resolved_model_override,
            resolved_codex_reasoning_effort,
            has_explicit_reasoning_override,
        )
    if not tenant_id:
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            resolved_model_override,
            default_codex_reasoning_effort,
            False,
        )
    if not database_url:
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            resolved_model_override,
            resolved_codex_reasoning_effort,
            has_explicit_reasoning_override,
        )

    (
        project_id,
        knowledge_enabled,
        knowledge_mode,
        resolved_model_override,
        codex_reasoning_effort,
        has_explicit_reasoning_override,
    ) = _resolve_knowledge_policy_for_context(context=context)
    if not knowledge_enabled:
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            resolved_model_override,
            codex_reasoning_effort,
            has_explicit_reasoning_override,
        )

    query_text = "\n".join(
        part for part in [str(context.command or ""), str(context.stage or ""), user_prompt, system_prompt[:1200]] if part.strip()
    )
    session_factory = create_session_factory(database_url=database_url)
    try:
        with session_factory() as session:
            context_payload = build_knowledge_prompt_context(
                session=session,
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key=context.issue_key,
                query=query_text,
                max_items=max(1, int(getattr(settings, "knowledge_context_top_k", 5))),
                max_chars=max(500, int(getattr(settings, "knowledge_context_max_chars", 3200))),
                embedding_access_mode=KnowledgeEmbeddingAccessMode.BEST_EFFORT,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "knowledge_context_lookup_failed tenant_id=%s project_id=%s command=%s stage=%s error=%s",
            tenant_id,
            project_id,
            context.command,
            context.stage,
            exc,
        )
        return (
            user_prompt,
            {"kb_lookup_attempted": True, "kb_hits": 0, "kb_context_chars": 0},
            resolved_model_override,
            codex_reasoning_effort,
            has_explicit_reasoning_override,
        )

    context_text = str(context_payload.text or "").strip()
    if not context_text:
        return (
            user_prompt,
            {"kb_lookup_attempted": True, "kb_hits": 0, "kb_context_chars": 0},
            resolved_model_override,
            codex_reasoning_effort,
            has_explicit_reasoning_override,
        )
    augmented = "\n\n".join(
        [
            user_prompt.strip(),
            "Knowledge Base context (project-scoped; prefer newer dated facts; cite assets when used):",
            context_text,
            f"Knowledge answer mode: {knowledge_mode}",
        ]
    ).strip()
    return augmented, {
        "kb_lookup_attempted": True,
        "kb_hits": len(context_payload.citations),
        "kb_context_chars": len(context_text),
    }, resolved_model_override, codex_reasoning_effort, has_explicit_reasoning_override


def _emit_invocation_event(
    *,
    context: AgentInvocationContext,
    event_kind: str,
    payload: dict[str, object],
) -> None:
    tenant_id = str(context.tenant_id or "").strip()
    if not tenant_id:
        return
    message = str(payload.get("message") or "").strip() or event_kind.replace("_", " ")
    event_payload = {
        "invocation_id": str(context.invocation_id or "").strip() or None,
        "channel": context.channel,
        "command": context.command,
        "working_dir": context.working_dir,
        "stage": context.stage,
        "attempt_id": context.attempt_id,
        "attempt": context.attempt,
        "stream": "system",
        **payload,
    }
    settings = get_settings()
    live_metadata: dict[str, object] = {
        "workflow_id": context.workflow_id,
        "operation_id": context.operation_id,
        "attempt_id": context.attempt_id,
        "run_id": context.run_id,
        "issue_key": context.issue_key,
        "invocation_id": str(context.invocation_id or "").strip() or None,
        "stage": context.stage,
        "attempt": context.attempt,
        "event_kind": event_kind,
    }
    for key, value in payload.items():
        if key == "message":
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            live_metadata[key] = value
            continue
        live_metadata[f"{key}_json"] = redact_sensitive_text(json.dumps(value, sort_keys=True, ensure_ascii=False))

    def _record(session: Session) -> None:
        row = record_observability_stream_event(
            session,
            tenant_id=tenant_id,
            project_id=context.project_id,
            workflow_id=context.workflow_id,
            run_id=context.run_id,
            operation_id=context.operation_id,
            attempt_id=context.attempt_id,
            issue_key=context.issue_key,
            source_component="runtime_invocation",
            event_kind=event_kind,
            level="info",
            message=message,
            payload=event_payload,
        )
        if row is None:
            raise RuntimeError("Runtime invocation event was not persisted")
        if str(context.operation_id or "").strip() and event_kind in {
            "stage_request",
            "stage_response",
            "tool_request",
            "tool_result",
            "stage_invocation_started",
            "stage_invocation_finished",
        }:
            record_audit_event(
                session,
                tenant_id=tenant_id,
                project_id=context.project_id,
                workflow_id=context.workflow_id,
                run_id=context.run_id,
                operation_id=context.operation_id,
                attempt_id=context.attempt_id,
                issue_key=context.issue_key,
                actor_type="agent",
                actor_id=settings.agent_id,
                source_component="runtime_invocation",
                event_kind=event_kind,
                level="info",
                message=message,
                payload=event_payload,
            )

    try:
        if context.db_session is not None:
            _record(context.db_session)
        else:
            session_factory = create_session_factory(database_url=settings.database_url)
            with session_factory() as session:
                _record(session)
                session.commit()
    except Exception as exc:  # noqa: BLE001
        redacted_error = redact_sensitive_text(str(exc))
        live_metadata["observability_persist_failed"] = True
        live_metadata["observability_persist_error"] = redacted_error
        logger.exception(
            "runtime_invocation_event_persist_failed event_kind=%s invocation_id=%s error=%s",
            event_kind,
            str(context.invocation_id or "").strip() or None,
            redacted_error,
        )
    _LIVE_INVOCATION_LOGGER.info(
        message,
        extra={
            "event_type": "runtime_invocation_event",
            "tenant_id": context.tenant_id,
            "project_id": context.project_id,
            "metadata": live_metadata,
        },
    )


def _checkpoint_kind_for_context(*, context: AgentInvocationContext) -> str | None:
    command = str(context.command or "").strip().lower()
    stage = str(context.stage or "").strip().lower()
    if command != "workflow":
        return None
    return checkpoint_kind_for_stage(stage)


def _persist_checkpoint_session_id(
    *,
    workflow_id: str | None,
    run_id: str | None,
    session_id: str | None,
    checkpoint_kind: str | None,
    stage: str | None,
) -> None:
    normalized_workflow_id = str(workflow_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_session_id = str(session_id or "").strip()
    normalized_kind = str(checkpoint_kind or "").strip().lower()
    if not normalized_workflow_id or not normalized_run_id or not normalized_session_id or not normalized_kind:
        return
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    try:
        with session_factory() as session:
            upsert_workflow_checkpoint(
                session,
                workflow_id=normalized_workflow_id,
                run_id=normalized_run_id,
                checkpoint_kind=normalized_kind,
                stage=normalize_checkpoint_stage(stage),
                payload=None,
                codex_session_id=normalized_session_id,
            )
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "codex_checkpoint_session_persist_skipped workflow_id=%s run_id=%s checkpoint_kind=%s session_id=%s error=%s",
            normalized_workflow_id,
            normalized_run_id,
            normalized_kind,
            normalized_session_id,
            exc,
        )


def _append_raw_log_line(
    *,
    context: AgentInvocationContext,
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


def _enqueue_runtime_log_line(*, context: AgentInvocationContext, stream: str, message: str) -> bool:
    try:
        if context.db_session is not None:
            _persist_runtime_log_line_with_session(session=context.db_session, context=context, stream=stream, message=message)
            return True
        writer = _get_log_writer()
        return writer.enqueue(context=context, stream=stream, message=message)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "runtime_log_enqueue_failed invocation_id=%s error=%s",
            str(context.invocation_id or "").strip() or None,
            redact_sensitive_text(str(exc)),
        )
        return False


def _persist_runtime_log_line_with_session(
    *,
    session: Session,
    context: AgentInvocationContext,
    stream: str,
    message: str,
) -> None:
    tenant_id = str(context.tenant_id or "").strip()
    if not tenant_id:
        raise ValueError("Runtime log persistence requires tenant_id")
    settings = get_settings()
    emit_logging_pane_event(
        session=session,
        tenant_id=tenant_id,
        project_id=str(context.project_id or "").strip() or None,
        workflow_id=str(context.workflow_id or "").strip() or None,
        operation_id=str(context.operation_id or "").strip() or None,
        attempt_id=str(context.attempt_id or "").strip() or None,
        run_id=str(context.run_id or "").strip() or None,
        issue_key=str(context.issue_key or "").strip() or None,
        agent_id=str(settings.agent_id),
        invocation_id=str(context.invocation_id or "").strip() or None,
        channel=str(context.channel or "").strip() or None,
        command=f"{context.command}.{context.stage}",
        working_dir=str(context.working_dir or "").strip() or None,
        stage=str(context.stage),
        attempt=context.attempt,
        stream=str(stream),
        message=str(message),
    )


def invoke_runtime_json(
    *,
    runtime: CodexRuntime,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    extra_on_log_line: Callable[[str, str], None] | None = None,
    allowed_native_tools: set[str] | None = None,
) -> dict:
    try:
        payload, _ = _invoke_runtime_json_once(
            runtime=runtime,
            context=context,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            extra_on_log_line=extra_on_log_line,
            allowed_native_tools=allowed_native_tools,
        )
    except NativeToolPolicyError as exc:
        payload, _ = _invoke_runtime_json_once(
            runtime=runtime,
            context=context,
            system_prompt=system_prompt,
            user_prompt=_build_native_tool_policy_repair_prompt(
                original_user_prompt=user_prompt,
                error=str(exc),
                allowed_native_tools=allowed_native_tools,
                stage=context.stage,
                runtime_command=str(getattr(runtime, "command", "") or ""),
                worker_platform=context.worker_platform,
            ),
            extra_on_log_line=extra_on_log_line,
            allowed_native_tools=allowed_native_tools,
        )
    return payload


def invoke_runtime_json_with_tools(
    *,
    runtime: CodexRuntime,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    allowed_tools: set[str],
    execute_tool: Callable[[str, dict[str, object]], dict[str, object]],
    extra_on_log_line: Callable[[str, str], None] | None = None,
    max_tool_hops: int = 8,
    required_tools: set[str] | None = None,
    allowed_native_tools: set[str] | None = None,
) -> dict:
    resume_session_id = str(context.codex_session_id or "").strip() or None
    current_user_prompt = user_prompt
    original_user_prompt = user_prompt
    normalized_allowed_tools = {str(tool).strip() for tool in allowed_tools if str(tool).strip()}
    normalized_required_tools = {str(tool).strip() for tool in required_tools or set() if str(tool).strip()}
    tool_hops_used = 0
    final_response_requested = False
    native_tool_policy_repair_requested = False
    governed_tool_availability_repair_requested = False

    for _tool_hop in range(max(0, int(max_tool_hops)) + 2):
        try:
            payload, observed_session_id = _invoke_runtime_json_once(
                runtime=runtime,
                context=AgentInvocationContext(
                    channel=context.channel,
                    tenant_id=context.tenant_id,
                    project_id=context.project_id,
                    command=context.command,
                    stage=context.stage,
                    working_dir=context.working_dir,
                    workflow_id=context.workflow_id,
                    operation_id=context.operation_id,
                    attempt_id=context.attempt_id,
                    issue_key=context.issue_key,
                    run_id=context.run_id,
                    attempt=context.attempt,
                    invocation_id=context.invocation_id,
                    reasoning_effort=context.reasoning_effort,
                    issue_description_chars=context.issue_description_chars,
                    codex_session_id=resume_session_id,
                    worker_platform=context.worker_platform,
                    db_session=context.db_session,
                ),
                system_prompt=system_prompt,
                user_prompt=current_user_prompt,
                extra_on_log_line=extra_on_log_line,
                allowed_native_tools=allowed_native_tools,
            )
        except NativeToolPolicyError as exc:
            if native_tool_policy_repair_requested:
                raise
            native_tool_policy_repair_requested = True
            current_user_prompt = _build_native_tool_policy_repair_prompt(
                original_user_prompt=original_user_prompt,
                error=str(exc),
                allowed_native_tools=allowed_native_tools,
                stage=context.stage,
                runtime_command=str(getattr(runtime, "command", "") or ""),
                worker_platform=context.worker_platform,
            )
            continue
        if observed_session_id:
            resume_session_id = observed_session_id

        response_type = str(payload.get("type") or "").strip().lower()
        if not response_type:
            return payload
        if response_type == _FINAL_RESPONSE_TYPE:
            result = payload.get("result")
            if isinstance(result, dict):
                if (
                    not governed_tool_availability_repair_requested
                    and tool_hops_used == 0
                    and _requires_governed_tool_availability_repair(
                        result=result,
                        allowed_tools=normalized_allowed_tools,
                    )
                ):
                    governed_tool_availability_repair_requested = True
                    current_user_prompt = _build_governed_tool_availability_repair_prompt(
                        original_user_prompt=original_user_prompt,
                        blocker_result=result,
                        allowed_tools=normalized_allowed_tools,
                    )
                    continue
                return result
            raise ToolBridgeProtocolError("Runtime tool bridge final_response must contain an object result")
        if response_type != _TOOL_REQUEST_TYPE:
            raise ToolBridgeProtocolError(f"Runtime tool bridge returned unsupported response type '{response_type}'")
        if tool_hops_used >= max_tool_hops:
            if final_response_requested:
                raise ToolBridgeExhaustedError("Runtime tool hop limit exceeded")
            final_response_requested = True
            current_user_prompt = _build_tool_result_prompt(
                tool_result={
                    "tool_name": str(payload.get("tool_name") or "").strip(),
                    "ok": False,
                    "failure_policy": "tool_hop_limit",
                    "error": f"Runtime tool hop limit {max_tool_hops} reached.",
                },
                require_final_response=True,
            )
            continue

        tool_hops_used += 1

        tool_name = str(payload.get("tool_name") or "").strip()
        if not tool_name:
            raise ToolBridgeProtocolError("Runtime tool bridge tool_request missing tool_name")
        if tool_name not in normalized_allowed_tools:
            raise ToolBridgeProtocolError(f"Runtime tool bridge requested disallowed tool '{tool_name}'")
        raw_tool_args = payload.get("tool_args")
        if raw_tool_args is None:
            tool_args: dict[str, object] = {}
        elif isinstance(raw_tool_args, dict):
            tool_args = dict(raw_tool_args)
        else:
            raise ToolBridgeProtocolError("Runtime tool bridge tool_request tool_args must be an object")
        _emit_invocation_event(
            context=context,
            event_kind="tool_request",
            payload={
                "message": f"Requested tool {tool_name}.",
                "tool_name": tool_name,
                "tool_hop": tool_hops_used,
                "tool_args": tool_args,
            },
        )

        try:
            tool_result = execute_tool(tool_name, tool_args)
            bridge_result: dict[str, object] = {
                "tool_name": tool_name,
                "ok": True,
                "result": tool_result,
            }
            _emit_invocation_event(
                context=context,
                event_kind="tool_result",
                payload={
                    "message": f"Completed tool {tool_name}.",
                    "tool_name": tool_name,
                    "tool_hop": tool_hops_used,
                    "ok": True,
                    "tool_result": tool_result,
                },
            )
        except (PermissionError, ValueError) as exc:
            bridge_result = {
                "tool_name": tool_name,
                "ok": False,
                "required": tool_name in normalized_required_tools,
                "failure_policy": "correctable_request",
                "error": str(exc),
            }
            _emit_invocation_event(
                context=context,
                event_kind="tool_result",
                payload={
                    "message": f"Tool {tool_name} failed with a correctable request error.",
                    "tool_name": tool_name,
                    "tool_hop": tool_hops_used,
                    "ok": False,
                    "error": str(exc),
                },
            )
        except Exception as exc:  # noqa: BLE001
            bridge_result = {
                "tool_name": tool_name,
                "ok": False,
                "required": tool_name in normalized_required_tools,
                "failure_policy": "recoverable_tool_failure",
                "error": str(exc),
            }
            _emit_invocation_event(
                context=context,
                event_kind="tool_result",
                payload={
                    "message": f"Tool {tool_name} failed with a recoverable execution error.",
                    "tool_name": tool_name,
                    "tool_hop": tool_hops_used,
                    "ok": False,
                    "error": str(exc),
                },
            )

        current_user_prompt = _build_tool_result_prompt(tool_result=bridge_result)

    raise ToolBridgeExhaustedError("Runtime tool bridge could not obtain a final response")


def _invoke_runtime_json_once(
    *,
    runtime: CodexRuntime,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    extra_on_log_line: Callable[[str, str], None] | None = None,
    allowed_native_tools: set[str] | None = None,
) -> tuple[dict, str | None]:
    (
        effective_user_prompt,
        knowledge_metrics,
        resolved_model_override,
        resolved_reasoning_effort,
        has_explicit_reasoning_override,
    ) = _augment_prompt_with_knowledge_context(
        context=context,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    if has_explicit_reasoning_override:
        effective_reasoning_effort = resolved_reasoning_effort
    else:
        effective_reasoning_effort = context.reasoning_effort or resolved_reasoning_effort
    invocation_id = context.invocation_id or uuid4().hex
    prompt_metrics = {
        "system_prompt_chars": len(system_prompt),
        "user_prompt_chars": len(effective_user_prompt),
        "estimated_prompt_tokens": _estimate_token_count(system_prompt) + _estimate_token_count(effective_user_prompt),
        "issue_description_chars": max(0, int(context.issue_description_chars or 0)),
    }
    context_metrics = _collect_context_injection_metrics(working_dir=context.working_dir)
    checkpoint_kind = _checkpoint_kind_for_context(context=context)
    resume_session_id = str(context.codex_session_id or "").strip() or None
    invocation_started_monotonic = time.monotonic()
    invocation_context = AgentInvocationContext(
        channel=context.channel,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        command=context.command,
        stage=context.stage,
        working_dir=context.working_dir,
        workflow_id=context.workflow_id,
        operation_id=context.operation_id,
        attempt_id=context.attempt_id,
        issue_key=context.issue_key,
        run_id=context.run_id,
        attempt=context.attempt,
        invocation_id=invocation_id,
        reasoning_effort=effective_reasoning_effort,
        issue_description_chars=context.issue_description_chars,
        codex_session_id=resume_session_id,
        db_session=context.db_session,
    )
    sink_state = {
        "turn_context_events": 0,
        "no_assistant_output_detected": False,
        "db_persisted_lines": 0,
        "raw_lines_written": 0,
        "codex_session_id": resume_session_id or "",
        "native_tool_policy_violations": "",
        "runtime_log_persistence_failures": 0,
        "runtime_log_persistence_error": "",
    }
    usage_state: dict[str, int | None] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cached_input_tokens": None,
    }
    runtime_model = str(getattr(runtime, "model", "") or "").strip()
    runtime_command = str(getattr(runtime, "command", "") or "").strip().lower()
    resolved_allowed_native_tools = _resolve_allowed_native_tools(
        allowed_native_tools=allowed_native_tools,
        stage=context.stage,
        runtime_command=runtime_command,
        worker_platform=context.worker_platform,
    )
    if runtime_command.startswith("http:") and runtime_model:
        resolved_model_override = runtime_model
    _emit_invocation_event(
        context=invocation_context,
        event_kind="stage_invocation_started",
        payload={
            "status": "started",
            "resumed_session": bool(resume_session_id),
            "codex_session_id": resume_session_id or "",
            "model": resolved_model_override,
            "reasoning_effort": effective_reasoning_effort,
            **prompt_metrics,
            **context_metrics,
            **knowledge_metrics,
        },
    )
    _emit_runtime_request_event(
        context=invocation_context,
        system_prompt=system_prompt,
        user_prompt=effective_user_prompt,
        require_json=True,
        resumed_session=bool(resume_session_id),
    )
    payload: dict | None = None
    failure_reason: str | None = None
    failure_payload_preview: str | None = None
    try:
        payload = runtime.run_json(
            system_prompt=system_prompt,
            user_prompt=effective_user_prompt,
            working_dir=context.working_dir,
            on_log_line=_combined_log_sink(
                context=invocation_context,
                extra_on_log_line=extra_on_log_line,
                sink_state=sink_state,
                usage_state=usage_state,
                allowed_native_tools=resolved_allowed_native_tools,
            ),
            reasoning_effort=effective_reasoning_effort,
            model_override=resolved_model_override,
            resume_session_id=resume_session_id,
            on_session_id=lambda session_id: _capture_session_id(
                context=invocation_context,
                sink_state=sink_state,
                session_id=session_id,
                checkpoint_kind=checkpoint_kind,
            ),
            on_usage=lambda usage: _capture_usage_metrics(
                usage_state=usage_state,
                usage=usage,
            ),
        )
        native_tool_policy_violations = str(sink_state.get("native_tool_policy_violations") or "").strip()
        if native_tool_policy_violations:
            raise NativeToolPolicyError(
                "Runtime used disallowed native tool(s): "
                f"{native_tool_policy_violations}. "
                "Native runtime tools must be enabled through the stage tool allowlist."
            )
        _emit_runtime_response_event(
            context=invocation_context,
            payload=payload,
        )
        return payload, str(sink_state.get("codex_session_id") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "payload_preview") and isinstance(exc.payload_preview, str):
            failure_payload_preview = exc.payload_preview
        failure_reason = str(exc)
        failure_reason_lower = failure_reason.lower()
        if "empty response" in failure_reason_lower:
            sink_state["no_assistant_output_detected"] = True
        if isinstance(exc, CodexRuntimeError) and any(marker in failure_reason_lower for marker in _JSON_PARSE_ERROR_MARKERS):
            raise RuntimeJsonContractError(f"Runtime did not return a JSON object: {failure_reason}") from exc
        raise
    finally:
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
                "runtime_log_persistence_failed": int(sink_state["runtime_log_persistence_failures"]) > 0,
                "runtime_log_persistence_failures": int(sink_state["runtime_log_persistence_failures"]),
                "runtime_log_persistence_error": str(sink_state["runtime_log_persistence_error"]),
                "resumed_session": bool(resume_session_id),
                "codex_session_id": str(sink_state.get("codex_session_id") or ""),
                "model": resolved_model_override,
                "reasoning_effort": effective_reasoning_effort,
                "output_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
                "error": redact_sensitive_text(failure_reason or ""),
                "failure_payload_preview": (
                    redact_sensitive_text(failure_payload_preview)
                    if failure_payload_preview is not None
                    else None
                ),
                "actual_prompt_tokens": usage_state.get("prompt_tokens"),
                "actual_completion_tokens": usage_state.get("completion_tokens"),
                "actual_total_tokens": usage_state.get("total_tokens"),
                "actual_cached_input_tokens": usage_state.get("cached_input_tokens"),
                "actual_usage_observed": any(value is not None for value in usage_state.values()),
                **prompt_metrics,
                **context_metrics,
                **knowledge_metrics,
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


def _build_tool_result_prompt(*, tool_result: dict[str, object], require_final_response: bool = False) -> str:
    continuation = "Continue from this result and return JSON only. "
    if tool_result.get("ok") is False:
        failure_policy = str(tool_result.get("failure_policy") or "").strip()
        if failure_policy == "correctable_request":
            continuation += (
                "This tool failure means the request shape or permission was invalid, not that the evidence is unavailable. "
            )
            if tool_result.get("required") is True:
                continuation += (
                    "This failed tool is required evidence. Issue a corrected allowed tool_request unless that is impossible; "
                    "if impossible, return a blocker that states the failed tool and the evidence still needed. "
                )
            else:
                continuation += (
                    "If this evidence is needed, issue a corrected allowed tool_request; otherwise explain why the evidence is unnecessary in the final_response. "
                )
        elif failure_policy == "recoverable_tool_failure":
            continuation += (
                "The allowed tool failed while executing. Treat this as recoverable: issue a corrected or narrower allowed tool_request if that can resolve it. "
                "If the tool is required and cannot be recovered, return a blocker that states the failed tool and the evidence still needed. "
            )
        elif failure_policy == "tool_hop_limit":
            continuation += (
                "The allowed tool-hop budget is exhausted for this invocation. Do not invent missing evidence. "
                "Return the best final_response from the evidence already gathered, or return a blocker that names the missing evidence. "
            )
        elif tool_result.get("required") is True:
            continuation += (
                "This failed tool is required evidence. Do not treat it as advisory; return a blocker unless a corrected allowed tool_request can obtain the same evidence. "
            )
        else:
            continuation += (
                "This failed tool is advisory only when independent evidence is sufficient; otherwise return a blocker that names the missing evidence. "
            )
    else:
        continuation += "Use the tool result as evidence for the next tool_request or final_response. "
    if require_final_response:
        continuation += "Do not issue another tool_request. Return a final_response now."
    else:
        continuation += "Return either another tool_request or a final_response."
    return (
        "Tool result:\n"
        f"{json.dumps(tool_result, sort_keys=True)}\n\n"
        f"{continuation}"
    )


def _build_native_tool_policy_repair_prompt(
    *,
    original_user_prompt: str,
    error: str,
    allowed_native_tools: set[str] | None,
    stage: str,
    runtime_command: str,
    worker_platform: str | None = None,
) -> str:
    resolved_allowed_native_tools = sorted(
        _resolve_allowed_native_tools(
            allowed_native_tools=allowed_native_tools,
            stage=stage,
            runtime_command=runtime_command,
            worker_platform=worker_platform,
        )
    )
    return (
        "Your previous response used a native runtime tool that is not allowed for this stage.\n"
        f"Policy error: {error}\n"
        f"Allowed native runtime tools for this stage: {json.dumps(resolved_allowed_native_tools)}\n\n"
        "Retry the task using only the allowed native runtime tools and any governed tool_request tools described in the original prompt. "
        "Do not rely on evidence gathered from the disallowed tool use. Return JSON only.\n\n"
        "Original prompt:\n"
        f"{original_user_prompt}"
    )


def _requires_governed_tool_availability_repair(
    *,
    result: dict[str, object],
    allowed_tools: set[str],
) -> bool:
    outcome = str(result.get("outcome") or "").strip().lower()
    if outcome != "blocked" or not allowed_tools:
        return False
    blocker_message = str(result.get("blocker_message") or "").strip()
    if not blocker_message:
        return False
    normalized_message = blocker_message.lower()
    if "tool_search returned 0 matching tools" in normalized_message:
        return True
    if "governed github/jira publication bridge" in normalized_message:
        return True
    if "does not expose a callable governed" in normalized_message:
        return True
    if (
        {"github.push_branch", "github.open_pr"} & {tool.lower() for tool in allowed_tools}
        and "governed pr publication" in normalized_message
        and (
            "could not be executed" in normalized_message
            or "not invokable" in normalized_message
            or "native codex tool path" in normalized_message
            or "native codex developer tools" in normalized_message
        )
    ):
        return True
    if "governed" not in normalized_message:
        return False
    availability_markers = (
        "not expose",
        "unavailable",
        "not available",
        "no matching tools",
        "0 matching tools",
        "cannot publish",
        "not invokable",
        "could not be executed",
        "native codex tool path",
        "native codex developer tools",
    )
    if not any(marker in normalized_message for marker in availability_markers):
        return False
    return any(tool_name.lower() in normalized_message for tool_name in allowed_tools)


def _build_governed_tool_availability_repair_prompt(
    *,
    original_user_prompt: str,
    blocker_result: dict[str, object],
    allowed_tools: set[str],
) -> str:
    return (
        "Your previous final_response incorrectly treated allowed governed tools as unavailable before issuing any "
        "`tool_request`.\n"
        f"Previous final_response.result: {json.dumps(blocker_result, sort_keys=True)}\n"
        f"Allowed governed tools for this invocation: {json.dumps(sorted(allowed_tools))}\n\n"
        "Retry from the original prompt. The allowed governed tool list is authoritative for this bridge. "
        "Do not use native `tool_search`, plugin discovery, or deferred tool lookup to decide whether an allowed "
        "governed tool exists. If you need one of the allowed governed tools, issue the corresponding `tool_request` "
        "directly. Only return `outcome=\"blocked\"` for tool unavailability after an actual `tool_request` fails. "
        "Return JSON only.\n\n"
        "Original prompt:\n"
        f"{original_user_prompt}"
    )


def _emit_runtime_request_event(
    *,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    require_json: bool,
    resumed_session: bool,
) -> None:
    _emit_invocation_event(
        context=context,
        event_kind="stage_request",
        payload={
            "message": "Submitted runtime request.",
            "require_json": require_json,
            "resumed_session": resumed_session,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        },
    )


def _emit_runtime_response_event(
    *,
    context: AgentInvocationContext,
    payload: dict[str, object],
) -> None:
    _emit_invocation_event(
        context=context,
        event_kind="stage_response",
        payload={
            "message": "Received runtime response.",
            "response_payload": payload,
        },
    )


def _capture_session_id(
    *,
    context: AgentInvocationContext,
    sink_state: dict[str, int | bool | str],
    session_id: str,
    checkpoint_kind: str | None,
) -> None:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return
    sink_state["codex_session_id"] = normalized_session_id
    _persist_checkpoint_session_id(
        workflow_id=context.workflow_id,
        run_id=context.run_id,
        session_id=normalized_session_id,
        checkpoint_kind=checkpoint_kind,
        stage=context.stage,
    )


def _capture_usage_metrics(*, usage_state: dict[str, int | None], usage: dict[str, int]) -> None:
    normalized_usage = normalize_runtime_token_usage(usage)
    if normalized_usage is None:
        return
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_input_tokens"):
        value = normalized_usage.get(key)
        if not isinstance(value, int) or value < 0:
            continue
        current = usage_state.get(key)
        if current is None or value >= current:
            usage_state[key] = value


_NATIVE_TOOL_ALIASES = {
    "web.search": "web.search",
    "web_search": "web.search",
    "web-search": "web.search",
    "web_search_call": "web.search",
    "web-search-call": "web.search",
    "web.fetch": "web.fetch",
    "web_fetch": "web.fetch",
    "web-fetch": "web.fetch",
    "browser.open": "browser.open",
    "browser_open": "browser.open",
    "browser-open": "browser.open",
    "browser.snapshot": "browser.snapshot",
    "browser_snapshot": "browser.snapshot",
    "browser-snapshot": "browser.snapshot",
}


def _normalize_native_tool_name(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    return _NATIVE_TOOL_ALIASES.get(normalized)


def _resolve_allowed_native_tools(
    *,
    allowed_native_tools: set[str] | None,
    stage: str,
    runtime_command: str,
    worker_platform: str | None = None,
) -> set[str]:
    if allowed_native_tools is not None:
        return {str(tool).strip() for tool in allowed_native_tools if str(tool).strip()}
    from orchestrator.core.runtime.tools import native_model_tools_for_stage

    return native_model_tools_for_stage(
        stage,
        runtime_command=runtime_command,
        worker_platform=worker_platform,
    )


def _native_tool_names_from_log_payload(payload: object) -> set[str]:
    return _native_tool_names_from_log_payload_with_depth(payload, depth=0)


def _native_tool_names_from_log_payload_with_depth(payload: object, *, depth: int) -> set[str]:
    observed: set[str] = set()
    if depth > 4:
        return observed
    if isinstance(payload, str):
        normalized = payload.strip()
        if normalized.startswith("{") or normalized.startswith("["):
            try:
                parsed_payload = json.loads(normalized)
            except json.JSONDecodeError:
                return observed
            return _native_tool_names_from_log_payload_with_depth(parsed_payload, depth=depth + 1)
        return observed
    if isinstance(payload, dict):
        for key in ("tool_name", "tool", "name", "type"):
            native_tool_name = _normalize_native_tool_name(payload.get(key))
            if native_tool_name is not None:
                observed.add(native_tool_name)
        for value in payload.values():
            observed.update(_native_tool_names_from_log_payload_with_depth(value, depth=depth + 1))
    elif isinstance(payload, list):
        for item in payload:
            observed.update(_native_tool_names_from_log_payload_with_depth(item, depth=depth + 1))
    return observed


def _record_native_tool_policy_observations(
    *,
    sink_state: dict[str, int | bool | str],
    message_text: str,
    allowed_native_tools: set[str],
) -> None:
    try:
        payload = json.loads(message_text)
    except json.JSONDecodeError:
        return
    observed = _native_tool_names_from_log_payload(payload)
    if not observed:
        return
    allowed = {str(tool).strip() for tool in allowed_native_tools if str(tool).strip()}
    disallowed = sorted(observed - allowed)
    if not disallowed:
        return
    existing = {
        item.strip()
        for item in str(sink_state.get("native_tool_policy_violations") or "").split(",")
        if item.strip()
    }
    existing.update(disallowed)
    sink_state["native_tool_policy_violations"] = ", ".join(sorted(existing))


def _combined_log_sink(
    *,
    context: AgentInvocationContext,
    extra_on_log_line: Callable[[str, str], None] | None,
    sink_state: dict[str, int | bool | str],
    usage_state: dict[str, int | None],
    allowed_native_tools: set[str] | None,
) -> Callable[[str, str], None]:
    telemetry_sink = build_runtime_log_sink(
        channel=context.channel,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        command=f"{context.command}.{context.stage}",
        workflow_id=context.workflow_id,
        operation_id=context.operation_id,
        attempt_id=context.attempt_id,
        run_id=context.run_id,
        attempt=context.attempt,
        issue_key=context.issue_key,
        working_dir=context.working_dir,
    )

    def _sink(stream: str, message: str) -> None:
        telemetry_sink(stream, message)
        message_text = str(message or "")
        _record_native_tool_policy_observations(
            sink_state=sink_state,
            message_text=message_text,
            allowed_native_tools=allowed_native_tools,
        )
        sanitized_message = redact_sensitive_text(message_text)
        parsed_usage = extract_turn_completed_usage(message_text)
        usage_from_line: dict[str, int] | None = None
        if parsed_usage is not None:
            usage_from_line = {
                "prompt_tokens": parsed_usage.input_tokens,
                "completion_tokens": parsed_usage.output_tokens,
                "total_tokens": parsed_usage.input_tokens + parsed_usage.output_tokens,
                "cached_input_tokens": parsed_usage.cached_input_tokens,
            }
        if usage_from_line is not None:
            _capture_usage_metrics(usage_state=usage_state, usage=usage_from_line)
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
            _append_raw_log_line(context=context, stream=stream, message=sanitized_message)
        if _enqueue_runtime_log_line(context=context, stream=stream, message=sanitized_message):
            sink_state["db_persisted_lines"] = int(sink_state.get("db_persisted_lines", 0)) + 1
        else:
            sink_state["runtime_log_persistence_failures"] = int(
                sink_state.get("runtime_log_persistence_failures", 0)
            ) + 1
            sink_state["runtime_log_persistence_error"] = "Runtime log write failed; see worker logs"
        if extra_on_log_line is not None:
            extra_on_log_line(stream, message)

    return _sink


def _persist_runtime_log_lines(*, items: list[_QueuedLogLine]) -> None:
    if not items:
        return
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    with session_factory() as session:
        batched_events: list[dict[str, object]] = []
        for item in items:
            tenant_id = str(item.context.tenant_id or "").strip()
            if not tenant_id:
                raise ValueError("Runtime log persistence requires tenant_id")
            batched_events.append(
                {
                    "tenant_id": tenant_id,
                    "project_id": item.context.project_id,
                    "workflow_id": item.context.workflow_id,
                    "operation_id": item.context.operation_id,
                    "attempt_id": item.context.attempt_id,
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
            emit_logging_pane_event(
                session=session,
                tenant_id=str(single["tenant_id"]),
                project_id=single["project_id"] if isinstance(single["project_id"], str) else None,
                workflow_id=single["workflow_id"] if isinstance(single["workflow_id"], str) else None,
                operation_id=single["operation_id"] if isinstance(single["operation_id"], str) else None,
                attempt_id=single["attempt_id"] if isinstance(single["attempt_id"], str) else None,
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
            emit_logging_pane_events_batch(
                session=session,
                events=batched_events,
            )
        session.commit()
