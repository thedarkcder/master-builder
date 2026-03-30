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

from orchestrator.core.codex_models import normalize_codex_reasoning_effort
from orchestrator.core.config import get_settings
from orchestrator.core.knowledge_base import build_knowledge_prompt_context
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.runtime_telemetry import build_runtime_log_sink
from orchestrator.core.run_logs import extract_turn_completed_usage
from orchestrator.core.run_logs import record_run_log_event, record_run_log_events_batch
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Run, Tenant

logger = logging.getLogger(__name__)
_ERROR_MARKERS = ("error", "failed", "fatal", "exception", "traceback")
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
    issue_key: str | None = None
    run_id: str | None = None
    attempt: int | None = None
    invocation_id: str | None = None
    reasoning_effort: str | None = None
    issue_description_chars: int | None = None
    codex_session_id: str | None = None


@dataclass(frozen=True)
class _QueuedLogLine:
    context: AgentInvocationContext
    stream: str
    message: str


class _AsyncRuntimeLogWriter:
    def __init__(self, *, max_queue_size: int = 2000) -> None:
        self._queue: Queue[_QueuedLogLine] = Queue(maxsize=max_queue_size)
        self._pending_counts: dict[str, int] = {}
        self._pending_lock = threading.Lock()
        self._pending_cond = threading.Condition(self._pending_lock)
        self._worker = threading.Thread(target=self._run, daemon=True, name="codex-log-writer")
        self._worker.start()
        self._dropped = 0
        settings = get_settings()
        self._batch_size = max(
            1,
            int(
                getattr(
                    settings,
                    "log_db_batch_size",
                    getattr(settings, "codex_log_batch_size", 50),
                )
            ),
        )
        self._batch_flush_ms = max(
            1,
            int(
                getattr(
                    settings,
                    "log_db_batch_flush_ms",
                    getattr(settings, "codex_log_batch_flush_ms", 50),
                )
            ),
        )

    def enqueue(self, *, context: AgentInvocationContext, stream: str, message: str) -> bool:
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
                _persist_runtime_log_lines(items=items)
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


def _resolve_knowledge_policy_for_context(
    *,
    context: AgentInvocationContext,
) -> tuple[str | None, bool, str, str, str, bool]:
    settings = get_settings()
    database_url = str(getattr(settings, "database_url", "") or "").strip()
    default_model = str(getattr(settings, "codex_model", "gpt-5.4") or "gpt-5.4")
    default_reasoning_effort = str(getattr(settings, "codex_reasoning_effort", "medium") or "medium")
    tenant_id = str(context.tenant_id or "").strip()
    if not tenant_id:
        return (
            context.project_id,
            bool(getattr(settings, "knowledge_base_enabled_default", True)),
            str(getattr(settings, "knowledge_auto_answer_mode_default", "aggressive")),
            default_model,
            default_reasoning_effort,
            False,
        )
    if not database_url:
        return (
            context.project_id,
            bool(getattr(settings, "knowledge_base_enabled_default", True)),
            str(getattr(settings, "knowledge_auto_answer_mode_default", "aggressive")),
            default_model,
            default_reasoning_effort,
            False,
        )

    session_factory = create_session_factory(database_url=database_url)
    resolved_project_id = context.project_id
    knowledge_enabled = bool(getattr(settings, "knowledge_base_enabled_default", True))
    knowledge_mode = str(getattr(settings, "knowledge_auto_answer_mode_default", "aggressive"))
    codex_model = default_model
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
                    codex_model,
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
                default_codex_model=default_model,
                default_codex_reasoning_effort=default_reasoning_effort,
            )
            knowledge_enabled = bool(effective.get("knowledge_base_enabled", knowledge_enabled))
            normalized_mode = str(effective.get("knowledge_auto_answer_mode") or "").strip().lower()
            if normalized_mode in {"safe", "balanced", "aggressive"}:
                knowledge_mode = normalized_mode
            codex_model = str(effective.get("codex_model") or codex_model).strip() or codex_model
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
        codex_model,
        codex_reasoning_effort,
        has_explicit_reasoning_override,
    )


def _augment_prompt_with_knowledge_context(
    *,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
) -> tuple[str, dict[str, object], str, str, bool]:
    settings = get_settings()
    default_codex_model = str(getattr(settings, "codex_model", "gpt-5.4") or "gpt-5.4")
    default_codex_reasoning_effort = str(getattr(settings, "codex_reasoning_effort", "medium") or "medium")
    database_url = str(getattr(settings, "database_url", "") or "").strip()
    tenant_id = str(context.tenant_id or "").strip()
    resolved_codex_model = default_codex_model
    resolved_codex_reasoning_effort = default_codex_reasoning_effort
    has_explicit_reasoning_override = False
    if tenant_id:
        (
            _,
            _,
            _,
            resolved_codex_model,
            resolved_codex_reasoning_effort,
            has_explicit_reasoning_override,
        ) = _resolve_knowledge_policy_for_context(
            context=context
        )
    if not bool(getattr(settings, "knowledge_injection_enabled", True)):
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            resolved_codex_model,
            resolved_codex_reasoning_effort,
            has_explicit_reasoning_override,
        )
    if not tenant_id:
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            default_codex_model,
            default_codex_reasoning_effort,
            False,
        )
    if not database_url:
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            resolved_codex_model,
            resolved_codex_reasoning_effort,
            has_explicit_reasoning_override,
        )

    (
        project_id,
        knowledge_enabled,
        knowledge_mode,
        codex_model,
        codex_reasoning_effort,
        has_explicit_reasoning_override,
    ) = _resolve_knowledge_policy_for_context(context=context)
    if not knowledge_enabled:
        return (
            user_prompt,
            {"kb_lookup_attempted": False, "kb_hits": 0, "kb_context_chars": 0},
            codex_model,
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
                query=query_text,
                max_items=max(1, int(getattr(settings, "knowledge_context_top_k", 5))),
                max_chars=max(500, int(getattr(settings, "knowledge_context_max_chars", 3200))),
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
            codex_model,
            codex_reasoning_effort,
            has_explicit_reasoning_override,
        )

    context_text = str(context_payload.text or "").strip()
    if not context_text:
        return (
            user_prompt,
            {"kb_lookup_attempted": True, "kb_hits": 0, "kb_context_chars": 0},
            codex_model,
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
    }, codex_model, codex_reasoning_effort, has_explicit_reasoning_override


def _emit_invocation_event(
    *,
    context: AgentInvocationContext,
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
            "runtime_invocation_telemetry_event_skipped event_kind=%s tenant_id=%s run_id=%s error=%s",
            event_kind,
            tenant_id,
            context.run_id,
            exc,
        )


def _session_column_for_context(*, context: AgentInvocationContext) -> str | None:
    command = str(context.command or "").strip().lower()
    stage = str(context.stage or "").strip().lower()
    if command != "workflow":
        return None
    if stage == "orchestrated_run":
        return "orchestrated_session_id"
    if stage == _WORKFLOW_STAGE_PM:
        return "pm_session_id"
    if stage in _WORKFLOW_EXECUTION_STAGES:
        return "dev_session_id"
    return None


def _load_run_session_id(*, run_id: str | None, session_column: str | None) -> str | None:
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


def _persist_run_session_id(*, run_id: str | None, session_id: str | None, session_column: str | None) -> None:
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


def _should_persist_db_line(
    *,
    stream: str,
    message: str,
    line_index: int,
    sample_every: int,
    persist_turn_completed_usage: bool,
) -> bool:
    if stream == "system":
        return True
    if persist_turn_completed_usage and extract_turn_completed_usage(message) is not None:
        return True
    if _is_error_like(message):
        return True
    if line_index <= 20:
        return True
    if sample_every <= 0:
        return False
    return line_index % sample_every == 0


def _enqueue_runtime_log_line(*, context: AgentInvocationContext, stream: str, message: str) -> None:
    writer = _get_log_writer()
    writer.enqueue(context=context, stream=stream, message=message)


def _is_recoverable_json_parse_failure(exc: Exception) -> bool:
    if not isinstance(exc, CodexRuntimeError):
        return False
    failure_reason_lower = str(exc).lower()
    return any(marker in failure_reason_lower for marker in _JSON_PARSE_ERROR_MARKERS)


def invoke_runtime_json(
    *,
    runtime: CodexRuntime,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    extra_on_log_line: Callable[[str, str], None] | None = None,
    require_json: bool = True,
) -> dict:
    payload, _ = _invoke_runtime_json_once(
        runtime=runtime,
        context=context,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        extra_on_log_line=extra_on_log_line,
        require_json=require_json,
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
    require_json: bool = True,
) -> dict:
    resume_session_id = str(context.codex_session_id or "").strip() or None
    current_user_prompt = user_prompt
    normalized_allowed_tools = {str(tool).strip() for tool in allowed_tools if str(tool).strip()}

    for tool_hop in range(max(0, int(max_tool_hops)) + 1):
        payload, observed_session_id = _invoke_runtime_json_once(
            runtime=runtime,
            context=AgentInvocationContext(
                channel=context.channel,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                command=context.command,
                stage=context.stage,
                working_dir=context.working_dir,
                issue_key=context.issue_key,
                run_id=context.run_id,
                attempt=context.attempt,
                invocation_id=context.invocation_id,
                reasoning_effort=context.reasoning_effort,
                issue_description_chars=context.issue_description_chars,
                codex_session_id=resume_session_id,
            ),
            system_prompt=system_prompt,
            user_prompt=current_user_prompt,
            extra_on_log_line=extra_on_log_line,
            require_json=require_json,
        )
        if observed_session_id:
            resume_session_id = observed_session_id

        response_type = str(payload.get("type") or "").strip().lower()
        if not response_type:
            return payload
        if response_type == _FINAL_RESPONSE_TYPE:
            result = payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex tool bridge final_response must contain an object result")
            return result
        if response_type != _TOOL_REQUEST_TYPE:
            raise RuntimeError(f"Codex tool bridge returned unsupported response type '{response_type}'")
        if tool_hop >= max_tool_hops:
            raise RuntimeError("Codex tool hop limit exceeded")

        tool_name = str(payload.get("tool_name") or "").strip()
        if not tool_name:
            raise RuntimeError("Codex tool bridge tool_request missing tool_name")
        if tool_name not in normalized_allowed_tools:
            raise RuntimeError(f"Codex tool bridge requested disallowed tool '{tool_name}'")
        raw_tool_args = payload.get("tool_args")
        if raw_tool_args is None:
            tool_args: dict[str, object] = {}
        elif isinstance(raw_tool_args, dict):
            tool_args = dict(raw_tool_args)
        else:
            raise RuntimeError("Codex tool bridge tool_request tool_args must be an object")

        try:
            tool_result = execute_tool(tool_name, tool_args)
            bridge_result: dict[str, object] = {
                "tool_name": tool_name,
                "ok": True,
                "result": tool_result,
            }
        except Exception as exc:  # noqa: BLE001
            bridge_result = {
                "tool_name": tool_name,
                "ok": False,
                "error": str(exc),
            }

        current_user_prompt = _build_tool_result_prompt(tool_result=bridge_result)

    raise RuntimeError("Codex tool hop limit exceeded")


def _invoke_runtime_json_once(
    *,
    runtime: CodexRuntime,
    context: AgentInvocationContext,
    system_prompt: str,
    user_prompt: str,
    extra_on_log_line: Callable[[str, str], None] | None = None,
    require_json: bool = True,
) -> tuple[dict, str | None]:
    (
        effective_user_prompt,
        knowledge_metrics,
        resolved_codex_model,
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
    session_column = _session_column_for_context(context=context)
    explicit_session_id = str(context.codex_session_id or "").strip() or None
    run_session_id = _load_run_session_id(run_id=context.run_id, session_column=session_column)
    resume_session_id = explicit_session_id or run_session_id
    invocation_started_monotonic = time.monotonic()
    invocation_context = AgentInvocationContext(
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
        reasoning_effort=effective_reasoning_effort,
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
    usage_state: dict[str, int | None] = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
    }
    runtime_command = str(getattr(runtime, "command", "") or "").strip().lower()
    runtime_model = str(getattr(runtime, "model", "") or "").strip()
    # HTTP runtimes (OpenAI-compatible providers including LM Studio) should
    # use their execution-profile model instead of codex policy defaults.
    resolved_model_override = (
        runtime_model
        if runtime_command.startswith("http:") and runtime_model
        else resolved_codex_model
    )
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
            ),
            reasoning_effort=effective_reasoning_effort,
            model_override=resolved_model_override,
            resume_session_id=resume_session_id,
            on_session_id=lambda session_id: _capture_session_id(
                context=invocation_context,
                sink_state=sink_state,
                session_id=session_id,
                session_column=session_column,
            ),
            on_usage=lambda usage: _capture_usage_metrics(
                usage_state=usage_state,
                usage=usage,
            ),
        )
        return payload, str(sink_state.get("codex_session_id") or "").strip() or None
    except Exception as exc:  # noqa: BLE001
        if hasattr(exc, "payload_preview") and isinstance(exc.payload_preview, str):
            failure_payload_preview = exc.payload_preview
        failure_reason = str(exc)
        failure_reason_lower = failure_reason.lower()
        if "empty response" in failure_reason_lower:
            sink_state["no_assistant_output_detected"] = True
        if not require_json and _is_recoverable_json_parse_failure(exc):
            return {
                "_raw_response": failure_payload_preview or "",
                "_parse_error": failure_reason,
                "_stage": context.stage,
                "_command": context.command,
            }, str(sink_state.get("codex_session_id") or "").strip() or None
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
                "model": resolved_codex_model,
                "reasoning_effort": effective_reasoning_effort,
                "output_keys": sorted(payload.keys()) if isinstance(payload, dict) else [],
                "error": failure_reason or "",
                "failure_payload_preview": failure_payload_preview,
                "actual_prompt_tokens": usage_state.get("prompt_tokens"),
                "actual_completion_tokens": usage_state.get("completion_tokens"),
                "actual_total_tokens": usage_state.get("total_tokens"),
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


def _build_tool_result_prompt(*, tool_result: dict[str, object]) -> str:
    return (
        "Tool result:\n"
        f"{json.dumps(tool_result, sort_keys=True)}\n\n"
        "Continue from this result and return JSON only. "
        "Return either another tool_request or a final_response."
    )


def _capture_session_id(
    *,
    context: AgentInvocationContext,
    sink_state: dict[str, int | bool | str],
    session_id: str,
    session_column: str | None,
) -> None:
    normalized_session_id = str(session_id or "").strip()
    if not normalized_session_id:
        return
    sink_state["codex_session_id"] = normalized_session_id
    _persist_run_session_id(
        run_id=context.run_id,
        session_id=normalized_session_id,
        session_column=session_column,
    )


def _capture_usage_metrics(*, usage_state: dict[str, int | None], usage: dict[str, int]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if not isinstance(value, int) or value < 0:
            continue
        current = usage_state.get(key)
        if current is None or value >= current:
            usage_state[key] = value


def _combined_log_sink(
    *,
    context: AgentInvocationContext,
    extra_on_log_line: Callable[[str, str], None] | None,
    sink_state: dict[str, int | bool],
    usage_state: dict[str, int | None],
) -> Callable[[str, str], None]:
    telemetry_sink = build_runtime_log_sink(
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
        parsed_usage = extract_turn_completed_usage(message_text)
        usage_from_line: dict[str, int] | None = None
        if parsed_usage is not None:
            usage_from_line = {
                "prompt_tokens": parsed_usage.input_tokens,
                "completion_tokens": parsed_usage.output_tokens,
                "total_tokens": parsed_usage.input_tokens + parsed_usage.output_tokens,
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
        _append_raw_log_line(context=context, stream=stream, message=message_text)
        settings = get_settings()
        sample_every = max(1, int(getattr(settings, "codex_db_log_sampling_interval", 100)))
        persist_turn_completed_usage = bool(
            getattr(settings, "codex_persist_turn_completed_usage", True)
        )
        if not _should_persist_db_line(
            stream=stream,
            message=message_text,
            line_index=line_counter,
            sample_every=sample_every,
            persist_turn_completed_usage=persist_turn_completed_usage,
        ):
            if extra_on_log_line is not None:
                extra_on_log_line(stream, message)
            return
        try:
            _enqueue_runtime_log_line(context=context, stream=stream, message=message_text)
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


def _persist_runtime_log_line(*, context: AgentInvocationContext, stream: str, message: str) -> None:
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
