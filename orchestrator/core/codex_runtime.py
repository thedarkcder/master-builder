from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from urllib import error as urllib_error
from urllib import request as urllib_request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from orchestrator.core.agent_execution_profiles import AgentExecutionProfile
from orchestrator.core.config import Settings
from orchestrator.core.platform_secret_service import platform_secret_service

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STDERR_ERROR_MARKERS = ("error", "failed", "fatal", "exception", "traceback")
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class CodexRuntimeError(RuntimeError):
    def __init__(self, message: str, payload_preview: str | None = None):
        super().__init__(message)
        self.payload_preview = payload_preview

    def __str__(self) -> str:
        base = str(self.args[0]) if self.args else ""
        preview = str(self.payload_preview or "").strip()
        if preview:
            return f"{base}: {preview}"
        return base or super().__str__()


def _openai_compatible_base_url_for_local_server(*, base_url: str, runtime_kind: str) -> str:
    """Ensure LM Studio / llama.cpp bases include /v1 before /chat/completions."""
    u = str(base_url or "").strip().rstrip("/")
    if not u:
        return u
    rk = str(runtime_kind or "").strip().lower()
    if rk in {"lm_studio", "llama_cpp"} and not re.search(r"/v\d+$", u):
        return f"{u}/v1"
    return u


def _shorten_preview(value: str, *, max_len: int = 2048) -> str:
    normalized = value if isinstance(value, str) else ""
    if len(normalized) <= max_len:
        return normalized
    head = max_len // 2
    tail = max(0, max_len - head - 5)
    return f"{normalized[:head]} ... {normalized[-tail:]}"


def _json_balanced_object_candidates(content: str) -> list[str]:
    candidates: list[str] = []
    text = content or ""
    index = 0
    while True:
        start = text.find("{", index)
        if start < 0:
            break
        depth = 0
        in_string = False
        escape_next = False
        end = -1
        for cursor in range(start, len(text)):
            char = text[cursor]
            if in_string:
                if escape_next:
                    escape_next = False
                elif char == "\\":
                    escape_next = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "{":
                depth += 1
                continue
            if char == "}":
                depth -= 1
                if depth == 0:
                    end = cursor
                    break
        if end < 0:
            break
        candidates.append(text[start : end + 1])
        index = end + 1
    return candidates


def _strip_ansi(text: str | bytes | None) -> str:
    if isinstance(text, bytes):
        normalized = text.decode("utf-8", errors="replace")
    else:
        normalized = str(text or "")
    return _ANSI_ESCAPE_PATTERN.sub("", normalized)


def build_cli_command_env(
    *,
    settings: Settings,
    working_dir: str | None,
    runtime_kind: str = "codex_cli",
) -> tuple[str | None, dict[str, str]]:
    command_cwd = str(working_dir).strip() if working_dir else None
    return command_cwd, _build_codex_subprocess_env(
        settings=settings,
        working_dir=command_cwd,
        runtime_kind=runtime_kind,
    )


def codex_login_status(
    *,
    codex_command: str,
    settings: Settings,
    working_dir: str | None,
) -> tuple[bool | None, str]:
    command_cwd, env = build_cli_command_env(settings=settings, working_dir=working_dir, runtime_kind="codex_cli")
    completed = subprocess.run(  # noqa: S603
        [codex_command, "login", "status"],
        capture_output=True,
        text=True,
        cwd=command_cwd or None,
        env=env,
        check=False,
    )
    combined_output = "\n".join(
        part for part in (_strip_ansi(completed.stdout), _strip_ansi(completed.stderr)) if part.strip()
    ).strip()
    if completed.returncode == 0:
        return True, combined_output
    if completed.returncode == 1 and "not logged in" in combined_output.lower():
        return False, combined_output
    return None, combined_output


@dataclass
class _HttpConversationSession:
    session_id: str
    runtime_kind: str
    system_prompt: str
    messages: list[dict[str, str]]


class _HttpConversationStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _HttpConversationSession] = {}

    def load(self, *, session_id: str, runtime_kind: str) -> _HttpConversationSession | None:
        normalized_session_id = str(session_id or "").strip()
        normalized_runtime_kind = str(runtime_kind or "").strip().lower()
        if not normalized_session_id or not normalized_runtime_kind:
            return None
        with self._lock:
            session = self._sessions.get(normalized_session_id)
            if session is None or session.runtime_kind != normalized_runtime_kind:
                return None
            return session

    def save(self, session: _HttpConversationSession) -> None:
        with self._lock:
            self._sessions[session.session_id] = session


_HTTP_CONVERSATION_STORE = _HttpConversationStore()


@dataclass(frozen=True)
class CodexRuntime:
    model: str
    max_output_tokens: int
    command: str
    _request: Callable[
        [
            str,
            str,
            str | None,
            Callable[[str, str], None] | None,
            str | None,
            str | None,
            Callable[[str], None] | None,
            Callable[[dict[str, int]], None] | None,
            str | None,
        ],
        str,
    ]

    def run_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        working_dir: str | None = None,
        on_log_line: Callable[[str, str], None] | None = None,
        reasoning_effort: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        on_session_id: Callable[[str], None] | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> str:
        try:
            output = self._request(
                system_prompt,
                user_prompt,
                working_dir,
                on_log_line,
                reasoning_effort,
                resume_session_id,
                on_session_id,
                on_usage,
                model_override,
            ).strip()
        except TypeError:
            try:
                output = self._request(  # type: ignore[misc]
                    system_prompt,
                    user_prompt,
                    working_dir,
                    on_log_line,
                    reasoning_effort,
                    resume_session_id,
                    on_session_id,
                    on_usage,
                ).strip()
            except TypeError:
                try:
                    output = self._request(  # type: ignore[misc]
                        system_prompt,
                        user_prompt,
                        working_dir,
                        on_log_line,
                        reasoning_effort,
                        resume_session_id,
                        on_session_id,
                    ).strip()
                except TypeError:
                    output = self._request(system_prompt, user_prompt, working_dir, on_log_line).strip()  # type: ignore[misc]
        if not output:
            raise CodexRuntimeError("Codex runtime returned an empty response")
        return output

    def run_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        working_dir: str | None = None,
        on_log_line: Callable[[str, str], None] | None = None,
        reasoning_effort: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        on_session_id: Callable[[str], None] | None = None,
        on_usage: Callable[[dict[str, int]], None] | None = None,
    ) -> dict:
        output = self.run_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            working_dir=working_dir,
            on_log_line=on_log_line,
            reasoning_effort=reasoning_effort,
            model_override=model_override,
            resume_session_id=resume_session_id,
            on_session_id=on_session_id,
            on_usage=on_usage,
        )
        try:
            payload = _extract_json_payload(output)
        except CodexRuntimeError as exc:
            preview = _shorten_preview(output, max_len=12000)
            raise CodexRuntimeError(
                f"{exc}", payload_preview=preview
            ) from exc
        if not isinstance(payload, dict):
            raise CodexRuntimeError(
                "Codex runtime did not return a JSON object",
                payload_preview=_shorten_preview(output, max_len=12000),
            )
        return payload



def _extract_json_payload(content: str) -> object:
    if not isinstance(content, str):
        raise CodexRuntimeError("Codex runtime response was not a string")

    raw = content.replace("\ufeff", "").replace("\x00", "").strip()
    stripped = _ANSI_ESCAPE_PATTERN.sub("", raw).strip()
    if not stripped:
        raise CodexRuntimeError("Codex runtime response did not include JSON")

    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Normalize model output that adds explanatory text before JSON blocks.
    first_object_char = -1
    for marker in ("{", "["):
        location = stripped.find(marker)
        if location >= 0 and (first_object_char < 0 or location < first_object_char):
            first_object_char = location
    if first_object_char > 0:
        stripped = stripped[first_object_char:]

    code_match = _JSON_BLOCK_PATTERN.search(stripped)
    if code_match:
        try:
            return json.loads(code_match.group(1))
        except json.JSONDecodeError as exc:
            raise CodexRuntimeError(f"Invalid JSON payload from Codex runtime: {exc}") from exc

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidates = _json_balanced_object_candidates(stripped)
        if not candidates:
            candidates = [stripped[start : end + 1]]
        last_line_error: json.JSONDecodeError | None = None
        parsed_objects: list[dict[str, object]] = []
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError as exc:
                last_line_error = exc
                continue
            if isinstance(parsed, dict):
                parsed_objects.append(parsed)
            if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
                parsed_objects.append(parsed[0])
        if parsed_objects:
            return parsed_objects[-1]
        parsed_line_objects: list[dict[str, object]] = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed_line = json.loads(line)
            except json.JSONDecodeError as line_exc:
                last_line_error = line_exc
                continue
            if isinstance(parsed_line, dict):
                parsed_line_objects.append(parsed_line)
            if isinstance(parsed_line, list) and len(parsed_line) == 1 and isinstance(parsed_line[0], dict):
                parsed_line_objects.append(parsed_line[0])
        if parsed_line_objects:
            return parsed_line_objects[-1]
        raise CodexRuntimeError(f"Invalid JSON payload from Codex runtime: {last_line_error}") from last_line_error

    raise CodexRuntimeError("Codex runtime response did not include JSON")


def _normalize_stderr_log_mode(raw_value: object) -> str:
    normalized = str(raw_value or "all").strip().lower()
    if normalized in {"all", "errors_only", "off"}:
        return normalized
    return "all"


def _should_emit_log_line(*, stream_name: str, line: str, stderr_log_mode: str) -> bool:
    if stream_name != "stderr":
        return True
    if stderr_log_mode == "off":
        return False
    if stderr_log_mode == "errors_only":
        lowered = line.lower()
        return any(marker in lowered for marker in _STDERR_ERROR_MARKERS)
    return True


def _extract_session_id_from_json_line(line: str) -> str | None:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    event_type = str(payload.get("type") or "").strip().lower()
    session_id = ""
    if event_type == "session_meta":
        event_payload = payload.get("payload")
        if isinstance(event_payload, dict):
            session_id = str(event_payload.get("id") or "").strip()
    elif event_type in {"thread.started", "thread.resumed"}:
        session_id = str(payload.get("thread_id") or "").strip()
    if not _UUID_PATTERN.match(session_id):
        return None
    return session_id


def _extract_last_message_from_json_stdout(lines: list[str]) -> str | None:
    last_message: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "").strip().lower()
        event_payload = payload.get("payload")
        item_payload = payload.get("item")
        if event_type == "event_msg" and isinstance(event_payload, dict):
            msg_type = str(event_payload.get("type") or "").strip().lower()
            if msg_type == "agent_message":
                message = str(event_payload.get("message") or "").strip()
                if message:
                    last_message = message
        if event_type == "item.completed" and isinstance(item_payload, dict):
            if str(item_payload.get("type") or "").strip().lower() == "agent_message":
                message = str(item_payload.get("text") or "").strip()
                if message:
                    last_message = message
        if event_type != "response_item" or not isinstance(event_payload, dict):
            continue
        if str(event_payload.get("type") or "").strip().lower() != "message":
            continue
        if str(event_payload.get("role") or "").strip().lower() != "assistant":
            continue
        content = event_payload.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "").strip().lower() != "output_text":
                continue
            text = str(item.get("text") or "").strip()
            if text:
                last_message = text
    return last_message


def _extract_terminal_error_from_json_stdout(lines: list[str]) -> str | None:
    last_error: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        event_type = str(payload.get("type") or "").strip().lower()
        if event_type == "error":
            message = str(payload.get("message") or "").strip()
            if message:
                last_error = message
                continue
        if event_type == "turn.failed":
            error_payload = payload.get("error")
            if isinstance(error_payload, dict):
                message = str(error_payload.get("message") or "").strip()
                if message:
                    last_error = message
                    continue
    return last_error


def _coerce_token_count(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        candidate = int(value)
        return candidate if candidate >= 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            candidate = int(text)
        except ValueError:
            return None
        return candidate if candidate >= 0 else None
    return None

def _extract_token_usage_from_dict(payload: dict[str, object]) -> dict[str, int] | None:
    prompt_tokens = _coerce_token_count(payload.get("prompt_tokens"))
    completion_tokens = _coerce_token_count(payload.get("completion_tokens"))
    total_tokens = _coerce_token_count(payload.get("total_tokens"))
    if prompt_tokens is None:
        prompt_tokens = _coerce_token_count(payload.get("input_tokens"))
    if completion_tokens is None:
        completion_tokens = _coerce_token_count(payload.get("output_tokens"))
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens
    if prompt_tokens is None and completion_tokens is None and total_tokens is None:
        return None
    usage: dict[str, int] = {}
    if prompt_tokens is not None:
        usage["prompt_tokens"] = prompt_tokens
    if completion_tokens is not None:
        usage["completion_tokens"] = completion_tokens
    if total_tokens is not None:
        usage["total_tokens"] = total_tokens
    return usage


def _extract_usage_from_json_stdout(lines: list[str]) -> dict[str, int] | None:
    best_usage: dict[str, int] | None = None
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        stack: list[object] = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                usage = _extract_token_usage_from_dict(current)
                if usage is not None:
                    if best_usage is None:
                        best_usage = usage
                    else:
                        best_total = best_usage.get("total_tokens")
                        usage_total = usage.get("total_tokens")
                        if usage_total is not None and (best_total is None or usage_total >= best_total):
                            best_usage = usage
                        elif best_total is None and len(usage) >= len(best_usage):
                            best_usage = usage
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    return best_usage


def _resolve_codex_tool_database_url(
    *,
    database_url: str | None,
    tool_database_url: str | None,
) -> str | None:
    explicit_tool_database_url = str(tool_database_url or "").strip()
    if explicit_tool_database_url:
        return explicit_tool_database_url
    normalized_database_url = str(database_url or "").strip()
    return normalized_database_url or None


def _ensure_http_conversation_session(
    *,
    runtime_kind: str,
    system_prompt: str,
    user_prompt: str,
    resume_session_id: str | None,
) -> _HttpConversationSession:
    normalized_runtime_kind = str(runtime_kind or "").strip().lower()
    normalized_resume_session_id = str(resume_session_id or "").strip()
    existing = _HTTP_CONVERSATION_STORE.load(
        session_id=normalized_resume_session_id,
        runtime_kind=normalized_runtime_kind,
    )
    if existing is not None:
        existing.messages.append({"role": "user", "content": user_prompt})
        _HTTP_CONVERSATION_STORE.save(existing)
        return existing

    session = _HttpConversationSession(
        session_id=normalized_resume_session_id or str(uuid4()),
        runtime_kind=normalized_runtime_kind,
        system_prompt=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    _HTTP_CONVERSATION_STORE.save(session)
    return session


def _running_inside_container() -> bool:
    return Path("/.dockerenv").exists()


def _runtime_home_root(*, settings: Settings) -> Path:
    configured_root = str(getattr(settings, "runtime_home", "") or "").strip()
    if configured_root:
        return Path(configured_root)
    normalized_home = str(os.environ.get("HOME") or "").strip()
    if _running_inside_container() and normalized_home:
        return Path(normalized_home)
    return Path(__file__).resolve().parents[2] / ".runtime-home"

def _runtime_home_for_kind(*, settings: Settings, runtime_kind: str) -> Path:
    return _runtime_home_root(settings=settings) / runtime_kind


def _seed_runtime_home_from_repo_config(*, runtime_home: Path) -> None:
    source_dir = Path(__file__).resolve().parents[2] / ".codex"
    if not source_dir.is_dir():
        return
    target_dir = runtime_home / ".codex"
    if target_dir.exists():
        return
    runtime_home.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)


def _build_codex_subprocess_env(
    *,
    settings: Settings,
    working_dir: str | None = None,
    runtime_kind: str = "codex_cli",
) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in ("LANG", "LC_ALL", "PATH", "SHELL", "TERM", "TMPDIR", "USER"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
    _ = working_dir
    subprocess_home = _runtime_home_for_kind(
        settings=settings,
        runtime_kind=str(runtime_kind or "codex_cli").strip().lower() or "codex_cli",
    )
    _seed_runtime_home_from_repo_config(runtime_home=subprocess_home)
    subprocess_home.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(subprocess_home)
    env["XDG_CONFIG_HOME"] = str(subprocess_home / ".config")
    tool_database_url = _resolve_codex_tool_database_url(
        database_url=str(getattr(settings, "database_url", "") or "").strip(),
        tool_database_url=str(getattr(settings, "codex_tool_database_url", "") or "").strip(),
    )
    if tool_database_url:
        env["ORCHESTRATOR_DATABASE_URL"] = tool_database_url
    return env


def build_codex_runtime(
    *,
    session: Session | None = None,
    settings: Settings,
    request_override: Callable[[str, str, str | None], str] | None = None,
) -> CodexRuntime:
    return build_cli_runtime(
        session=session,
        settings=settings,
        request_override=request_override,
        cli_command_override=settings.codex_cli_command,
        runtime_kind_override="codex_cli",
        default_model_override=settings.codex_model,
        default_reasoning_effort_override=settings.codex_reasoning_effort,
    )


def build_runtime_for_execution_profile(
    *,
    session: Session | None = None,
    settings: Settings,
    profile: AgentExecutionProfile,
    request_override: Callable[[str, str, str | None], str] | None = None,
) -> CodexRuntime:
    normalized_runtime_kind = str(profile.runtime_kind or "").strip().lower()
    if normalized_runtime_kind in {"openai", "claude", "llama_cpp", "lm_studio"}:
        api_key = None
        if profile.api_key_secret_ref:
            if session is None:
                raise CodexRuntimeError("Session is required to resolve runtime API credentials")
            api_key = platform_secret_service.get(
                session=session,
                secret_ref=profile.api_key_secret_ref,
                encryption_key=settings.secrets_encryption_key,
            )
            if api_key is None:
                raise CodexRuntimeError(
                    f"Runtime API credential could not be resolved for secret ref '{profile.api_key_secret_ref}'"
                )
        return build_http_runtime(
            settings=settings,
            runtime_kind=normalized_runtime_kind,
            base_url=profile.base_url,
            api_key=api_key,
            request_override=request_override,
            default_model_override=str(profile.model or "").strip() or settings.codex_model,
            default_reasoning_effort_override=(
                str(profile.reasoning_effort or "").strip().lower() or settings.codex_reasoning_effort
            ),
        )
    normalized_cli_command = str(profile.cli_command or "").strip() or settings.codex_cli_command
    normalized_model = str(profile.model or "").strip() or settings.codex_model
    normalized_reasoning_effort = (
        str(profile.reasoning_effort or "").strip().lower()
        or settings.codex_reasoning_effort
    )
    return build_cli_runtime(
        session=session,
        settings=settings,
        request_override=request_override,
        cli_command_override=normalized_cli_command,
        runtime_kind_override=normalized_runtime_kind,
        default_model_override=normalized_model,
        default_reasoning_effort_override=normalized_reasoning_effort,
    )


def _http_json_request(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib_request.Request(
        url=url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **headers,
        },
        method=method,
    )
    try:
        with urllib_request.urlopen(request, timeout=120) as response:
            raw_body = response.read().decode("utf-8")
    except urllib_error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise CodexRuntimeError(
            f"Runtime HTTP request failed with status {exc.code}",
            payload_preview=_shorten_preview(error_body, max_len=4000),
        ) from exc
    except urllib_error.URLError as exc:
        raise CodexRuntimeError(f"Runtime HTTP request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise CodexRuntimeError(
            "Runtime HTTP request did not return valid JSON",
            payload_preview=_shorten_preview(raw_body, max_len=4000),
        ) from exc
    if not isinstance(parsed, dict):
        raise CodexRuntimeError(
            "Runtime HTTP request did not return a JSON object",
            payload_preview=_shorten_preview(raw_body, max_len=4000),
        )
    return parsed


def _extract_openai_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CodexRuntimeError("OpenAI-compatible runtime returned no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                texts.append(str(item.get("text") or ""))
        joined = "".join(texts).strip()
        if joined:
            return joined
    raise CodexRuntimeError("OpenAI-compatible runtime returned no text content")


def _extract_claude_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        raise CodexRuntimeError("Claude runtime returned no content")
    texts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(str(item.get("text") or ""))
    joined = "".join(texts).strip()
    if not joined:
        raise CodexRuntimeError("Claude runtime returned no text content")
    return joined


def build_http_runtime(
    *,
    settings: Settings,
    runtime_kind: str,
    base_url: str | None,
    api_key: str | None,
    request_override: Callable[[str, str, str | None], str] | None = None,
    default_model_override: str | None = None,
    default_reasoning_effort_override: str | None = None,
) -> CodexRuntime:
    if request_override is not None:
        return build_cli_runtime(
            settings=settings,
            request_override=request_override,
            cli_command_override="override",
            default_model_override=default_model_override,
            default_reasoning_effort_override=default_reasoning_effort_override,
        )

    normalized_runtime_kind = str(runtime_kind or "").strip().lower()
    normalized_model = str(default_model_override or settings.codex_model or "").strip() or settings.codex_model
    normalized_reasoning_effort = (
        str(default_reasoning_effort_override or settings.codex_reasoning_effort or "").strip().lower()
        or settings.codex_reasoning_effort
    )
    normalized_base_url = str(base_url or "").strip()
    if normalized_runtime_kind == "openai":
        normalized_base_url = normalized_base_url or "https://api.openai.com/v1"
    elif normalized_runtime_kind == "claude":
        normalized_base_url = normalized_base_url or "https://api.anthropic.com"
    if not normalized_base_url:
        raise CodexRuntimeError(f"Base URL is required for runtime kind '{normalized_runtime_kind}'")
    chat_completions_base = _openai_compatible_base_url_for_local_server(
        base_url=normalized_base_url,
        runtime_kind=normalized_runtime_kind,
    )

    def _request(
        system_prompt: str,
        user_prompt: str,
        working_dir: str | None,
        on_log_line: Callable[[str, str], None] | None,
        reasoning_effort: str | None,
        resume_session_id: str | None,
        on_session_id: Callable[[str], None] | None,
        on_usage: Callable[[dict[str, int]], None] | None,
        model_override: str | None,
    ) -> str:
        _ = working_dir
        resolved_model = str(model_override or normalized_model).strip() or normalized_model
        resolved_reasoning_effort = str(reasoning_effort or normalized_reasoning_effort).strip().lower() or normalized_reasoning_effort
        session = _ensure_http_conversation_session(
            runtime_kind=normalized_runtime_kind,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            resume_session_id=resume_session_id,
        )
        if on_session_id is not None:
            on_session_id(session.session_id)
        if on_log_line is not None:
            on_log_line(
                "stdout",
                f"[runtime:{normalized_runtime_kind}] model={resolved_model} reasoning={resolved_reasoning_effort} session={session.session_id}",
            )
        if normalized_runtime_kind in {"openai", "llama_cpp", "lm_studio"}:
            headers: dict[str, str] = {}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            payload = _http_json_request(
                method="POST",
                url=f"{chat_completions_base.rstrip('/')}/chat/completions",
                headers=headers,
                payload={
                    "model": resolved_model,
                    "messages": [
                        {"role": "system", "content": session.system_prompt},
                        *session.messages,
                    ],
                    "temperature": 0,
                    "max_tokens": settings.codex_max_output_tokens,
                },
            )
            usage = payload.get("usage")
            if on_usage is not None and isinstance(usage, dict):
                on_usage({key: int(value) for key, value in usage.items() if isinstance(value, (int, float))})
            response_text = _extract_openai_text(payload)
            session.messages.append({"role": "assistant", "content": response_text})
            _HTTP_CONVERSATION_STORE.save(session)
            return response_text
        if normalized_runtime_kind == "claude":
            headers = {
                "anthropic-version": "2023-06-01",
            }
            if api_key:
                headers["x-api-key"] = api_key
            payload = _http_json_request(
                method="POST",
                url=f"{normalized_base_url.rstrip('/')}/v1/messages",
                headers=headers,
                payload={
                    "model": resolved_model,
                    "system": session.system_prompt,
                    "max_tokens": settings.codex_max_output_tokens,
                    "messages": session.messages,
                },
            )
            usage = payload.get("usage")
            if on_usage is not None and isinstance(usage, dict):
                on_usage({key: int(value) for key, value in usage.items() if isinstance(value, (int, float))})
            response_text = _extract_claude_text(payload)
            session.messages.append({"role": "assistant", "content": response_text})
            _HTTP_CONVERSATION_STORE.save(session)
            return response_text
        raise CodexRuntimeError(f"Unsupported HTTP runtime kind '{normalized_runtime_kind}'")

    return CodexRuntime(
        model=normalized_model,
        max_output_tokens=settings.codex_max_output_tokens,
        command=f"http:{normalized_runtime_kind}",
        _request=_request,
    )


def build_runtime_with_fallback(
    *,
    primary_runtime: CodexRuntime,
    fallback_runtime: CodexRuntime,
) -> CodexRuntime:
    def _request_with_fallback(
        system_prompt: str,
        user_prompt: str,
        working_dir: str | None,
        on_log_line: Callable[[str, str], None] | None,
        reasoning_effort: str | None,
        resume_session_id: str | None,
        on_session_id: Callable[[str], None] | None,
        on_usage: Callable[[dict[str, int]], None] | None,
        model_override: str | None,
    ) -> str:
        try:
            return primary_runtime._request(
                system_prompt,
                user_prompt,
                working_dir,
                on_log_line,
                reasoning_effort,
                resume_session_id,
                on_session_id,
                on_usage,
                model_override,
            )
        except CodexRuntimeError:
            return fallback_runtime._request(
                system_prompt,
                user_prompt,
                working_dir,
                on_log_line,
                reasoning_effort,
                resume_session_id,
                on_session_id,
                on_usage,
                model_override,
            )

    return CodexRuntime(
        model=primary_runtime.model,
        max_output_tokens=primary_runtime.max_output_tokens,
        command=f"{primary_runtime.command}||{fallback_runtime.command}",
        _request=_request_with_fallback,
    )


def build_cli_runtime(
    *,
    session: Session | None = None,
    settings: Settings,
    request_override: Callable[[str, str, str | None], str] | None = None,
    cli_command_override: str | None = None,
    runtime_kind_override: str | None = None,
    default_model_override: str | None = None,
    default_reasoning_effort_override: str | None = None,
) -> CodexRuntime:
    _ = session
    if request_override is not None:
        def _request_with_override(
            system_prompt: str,
            user_prompt: str,
            working_dir: str | None,
            on_log_line: Callable[[str, str], None] | None,
            reasoning_effort: str | None,
            resume_session_id: str | None,
            on_session_id: Callable[[str], None] | None,
            on_usage: Callable[[dict[str, int]], None] | None,
            model_override: str | None,
        ) -> str:
            _ = on_log_line
            _ = working_dir
            _ = reasoning_effort
            _ = resume_session_id
            _ = on_session_id
            _ = on_usage
            _ = model_override
            try:
                return request_override(system_prompt, user_prompt, working_dir)
            except TypeError:
                return request_override(system_prompt, user_prompt)  # type: ignore[misc]

        return CodexRuntime(
            model=str(default_model_override or settings.codex_model or "").strip() or settings.codex_model,
            max_output_tokens=settings.codex_max_output_tokens,
            command="override",
            _request=_request_with_override,
        )

    codex_command = str(cli_command_override or settings.codex_cli_command or "").strip()
    if not codex_command:
        raise CodexRuntimeError("Codex CLI command is not configured")
    if shutil.which(codex_command) is None:
        raise CodexRuntimeError(
            f"Codex CLI command '{codex_command}' was not found in PATH"
        )
    def _request(
        system_prompt: str,
        user_prompt: str,
        working_dir: str | None,
        on_log_line: Callable[[str, str], None] | None,
        reasoning_effort: str | None,
        resume_session_id: str | None,
        on_session_id: Callable[[str], None] | None,
        on_usage: Callable[[dict[str, int]], None] | None,
        model_override: str | None,
    ) -> str:
        combined_prompt = (
            "You are the Codex orchestration runtime. "
            "Follow the system instructions exactly and return only the required output.\n\n"
            "IMPORTANT: When parsing a JSON payload is expected, respond with a single JSON object only.\n\n"
            f"## System instructions\n{system_prompt}\n\n"
            f"## User request\n{user_prompt}"
        )
        command_cwd = str(working_dir).strip() if working_dir else None
        subprocess_env = _build_codex_subprocess_env(
            settings=settings,
            working_dir=command_cwd,
            runtime_kind=str(runtime_kind_override or "codex_cli").strip().lower() or "codex_cli",
        )
        stderr_log_mode = _normalize_stderr_log_mode(getattr(settings, "codex_stderr_log_mode", "all"))
        normalized_reasoning_effort = str(reasoning_effort or settings.codex_reasoning_effort).strip().lower()
        if normalized_reasoning_effort not in {"low", "medium", "high"}:
            normalized_reasoning_effort = (
                str(default_reasoning_effort_override or settings.codex_reasoning_effort).strip().lower()
                or settings.codex_reasoning_effort
            )
        normalized_resume_session_id = str(resume_session_id or "").strip()
        default_model = str(default_model_override or settings.codex_model).strip() or settings.codex_model
        resolved_model = str(model_override or default_model).strip() or default_model
        session_callback_invoked = False
        command: list[str]
        normalized_sandbox_mode = str(settings.codex_sandbox_mode or "").strip().lower()
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", suffix=".txt") as output_file:
            if normalized_resume_session_id:
                command = [
                    codex_command,
                    "exec",
                    "resume",
                    normalized_resume_session_id,
                    "--disable",
                    "apps",
                    "--disable",
                    "plugins",
                    "--skip-git-repo-check",
                ]
                if normalized_sandbox_mode == "danger-full-access":
                    command.append("--dangerously-bypass-approvals-and-sandbox")
                elif normalized_sandbox_mode == "workspace-write":
                    command.append("--full-auto")
                command.extend(
                    [
                        "-c",
                        f'reasoning.effort="{normalized_reasoning_effort}"',
                        "--model",
                        resolved_model,
                        "--json",
                        "-",
                    ]
                )
            else:
                command = [
                    codex_command,
                    "exec",
                    "--disable",
                    "apps",
                    "--disable",
                    "plugins",
                    "--skip-git-repo-check",
                    "--sandbox",
                    settings.codex_sandbox_mode,
                    "-c",
                    f'reasoning.effort="{normalized_reasoning_effort}"',
                    "--color",
                    "never",
                    "--model",
                    resolved_model,
                    "--json",
                    "--output-last-message",
                    output_file.name,
                    "-",
                ]
            process = subprocess.Popen(  # noqa: S603
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=command_cwd or None,
                env=subprocess_env,
            )
            stdout_lines: list[str] = []
            stderr_lines: list[str] = []
            activity_lock = threading.Lock()
            last_activity_monotonic = time.monotonic()

            def _mark_activity() -> None:
                nonlocal last_activity_monotonic
                with activity_lock:
                    last_activity_monotonic = time.monotonic()

            def _current_idle_seconds() -> float:
                with activity_lock:
                    return max(0.0, time.monotonic() - last_activity_monotonic)

            def _consume(pipe, stream_name: str, collector: list[str]) -> None:  # noqa: ANN001
                nonlocal session_callback_invoked
                for line in iter(pipe.readline, ""):
                    collector.append(line)
                    _mark_activity()
                    if stream_name == "stdout" and on_session_id is not None and not session_callback_invoked:
                        discovered_session_id = _extract_session_id_from_json_line(line)
                        if discovered_session_id:
                            on_session_id(discovered_session_id)
                            session_callback_invoked = True
                    if on_log_line is not None:
                        text_line = line.rstrip("\n")
                        if text_line and _should_emit_log_line(
                            stream_name=stream_name,
                            line=text_line,
                            stderr_log_mode=stderr_log_mode,
                        ):
                            on_log_line(stream_name, text_line)
                pipe.close()

            stdout_thread = threading.Thread(
                target=_consume,
                args=(process.stdout, "stdout", stdout_lines),  # type: ignore[arg-type]
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_consume,
                args=(process.stderr, "stderr", stderr_lines),  # type: ignore[arg-type]
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()
            assert process.stdin is not None
            process.stdin.write(combined_prompt)
            process.stdin.close()
            _mark_activity()
            quiet_threshold_seconds = max(
                30,
                int(getattr(settings, "codex_hang_detection_quiet_seconds", 300)),
            )
            report_interval_seconds = max(
                15,
                int(getattr(settings, "codex_hang_detection_report_interval_seconds", 120)),
            )
            suspected_hung = False
            next_idle_report_at_monotonic: float | None = None
            while True:
                try:
                    returncode = process.wait(timeout=1.0)
                    break
                except subprocess.TimeoutExpired:
                    idle_seconds = _current_idle_seconds()
                    if idle_seconds < quiet_threshold_seconds:
                        continue
                    if not suspected_hung:
                        suspected_hung = True
                        next_idle_report_at_monotonic = time.monotonic() + report_interval_seconds
                        if on_log_line is not None:
                            on_log_line(
                                "system",
                                "codex_process_suspected_hung "
                                f"idle_seconds={int(idle_seconds)} "
                                f"quiet_threshold_seconds={quiet_threshold_seconds} "
                                f"pid={getattr(process, 'pid', 'unknown')}",
                            )
                        continue
                    if (
                        next_idle_report_at_monotonic is not None
                        and time.monotonic() >= next_idle_report_at_monotonic
                    ):
                        next_idle_report_at_monotonic = time.monotonic() + report_interval_seconds
                        if on_log_line is not None:
                            on_log_line(
                                "system",
                                "codex_process_still_idle "
                                f"idle_seconds={int(idle_seconds)} "
                                f"report_interval_seconds={report_interval_seconds} "
                                f"pid={getattr(process, 'pid', 'unknown')}",
                            )
            stdout_thread.join()
            stderr_thread.join()
            process_stdout = "".join(stdout_lines)
            process_stderr = "".join(stderr_lines)
            if returncode != 0:
                stderr = (process_stderr or "").strip()
                structured_error = _extract_terminal_error_from_json_stdout(stdout_lines)
                error_message = structured_error or stderr or "no stderr"
                login_status, _ = codex_login_status(
                    codex_command=codex_command,
                    settings=settings,
                    working_dir=command_cwd,
                )
                if login_status is False:
                    raise CodexRuntimeError(
                        "Codex CLI is not authenticated on this worker. "
                        "Start a worker runtime login session and retry."
                    )
                raise CodexRuntimeError(
                    f"Codex CLI command failed (exit={returncode}): {error_message}"
                )
            output_file.seek(0)
            usage = _extract_usage_from_json_stdout(stdout_lines)
            if usage is not None and on_usage is not None:
                try:
                    on_usage(usage)
                except Exception:  # noqa: BLE001
                    if on_log_line is not None:
                        on_log_line("system", "codex_usage_callback_failed")
            if not normalized_resume_session_id:
                output = output_file.read().strip()
                if output:
                    return output
            json_output = _extract_last_message_from_json_stdout(stdout_lines)
            if json_output:
                return json_output
            stdout = process_stdout.strip()
            if stdout:
                return stdout
            raise CodexRuntimeError("Codex CLI returned empty output")

    return CodexRuntime(
        model=str(default_model_override or settings.codex_model or "").strip() or settings.codex_model,
        max_output_tokens=settings.codex_max_output_tokens,
        command=codex_command,
        _request=_request,
    )
