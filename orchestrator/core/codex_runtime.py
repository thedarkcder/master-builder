from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestrator.core.agent_execution_profiles import AgentExecutionProfile
from orchestrator.core.config import Settings

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_STDERR_ERROR_MARKERS = ("error", "failed", "fatal", "exception", "traceback")
_URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class CodexRuntimeError(RuntimeError):
    def __init__(self, message: str, payload_preview: str | None = None):
        super().__init__(message)
        self.payload_preview = payload_preview


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


def _extract_first_url(content: str) -> str | None:
    match = _URL_PATTERN.search(content or "")
    if match is None:
        return None
    return match.group(0).strip()


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


def _build_codex_subprocess_env(*, settings: Settings) -> dict[str, str]:
    env = os.environ.copy()
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
        default_model_override=normalized_model,
        default_reasoning_effort_override=normalized_reasoning_effort,
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
    default_model_override: str | None = None,
    default_reasoning_effort_override: str | None = None,
) -> CodexRuntime:
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
    subprocess_env = _build_codex_subprocess_env(settings=settings)

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
                if "login" in error_message.lower() or "auth" in error_message.lower():
                    auth_url = _extract_first_url(error_message)
                    auth_link = f" Open this link to authenticate: {auth_url}." if auth_url else ""
                    message = (
                        "Codex CLI is not authenticated."
                        f"{auth_link} "
                        "Run `docker compose run --rm worker-runtime codex login --device-auth`."
                    )
                    raise CodexRuntimeError(message)
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
