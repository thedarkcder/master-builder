from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestrator.core.config import Settings

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_STDERR_ERROR_MARKERS = ("error", "failed", "fatal", "exception", "traceback")
_URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")
_UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class CodexRuntimeError(RuntimeError):
    pass


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
        resume_session_id: str | None = None,
        on_session_id: Callable[[str], None] | None = None,
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
        resume_session_id: str | None = None,
        on_session_id: Callable[[str], None] | None = None,
    ) -> dict:
        output = self.run_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            working_dir=working_dir,
            on_log_line=on_log_line,
            reasoning_effort=reasoning_effort,
            resume_session_id=resume_session_id,
            on_session_id=on_session_id,
        )
        payload = _extract_json_payload(output)
        if not isinstance(payload, dict):
            raise CodexRuntimeError("Codex runtime did not return a JSON object")
        return payload



def _extract_json_payload(content: str) -> object:
    stripped = content.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    code_match = _JSON_BLOCK_PATTERN.search(stripped)
    if code_match:
        try:
            return json.loads(code_match.group(1))
        except json.JSONDecodeError as exc:
            raise CodexRuntimeError(f"Invalid JSON payload from Codex runtime: {exc}") from exc

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        candidate = stripped[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_line_error: json.JSONDecodeError | None = exc
            last_dict_payload: dict | None = None
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
                    last_dict_payload = parsed_line
            if last_dict_payload is not None:
                return last_dict_payload
            raise CodexRuntimeError(f"Invalid JSON payload from Codex runtime: {last_line_error}") from exc

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


def build_codex_runtime(
    *,
    session: Session | None = None,
    settings: Settings,
    request_override: Callable[[str, str, str | None], str] | None = None,
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
        ) -> str:
            _ = on_log_line
            _ = working_dir
            _ = reasoning_effort
            _ = resume_session_id
            _ = on_session_id
            try:
                return request_override(system_prompt, user_prompt, working_dir)
            except TypeError:
                return request_override(system_prompt, user_prompt)  # type: ignore[misc]

        return CodexRuntime(
            model=settings.codex_model,
            max_output_tokens=settings.codex_max_output_tokens,
            command="override",
            _request=_request_with_override,
        )

    codex_command = (settings.codex_cli_command or "").strip()
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
    ) -> str:
        combined_prompt = (
            "You are the Codex orchestration runtime. "
            "Follow the system instructions exactly and return only the required output.\n\n"
            f"## System instructions\n{system_prompt}\n\n"
            f"## User request\n{user_prompt}"
        )
        command_cwd = str(working_dir).strip() if working_dir else None
        stderr_log_mode = _normalize_stderr_log_mode(getattr(settings, "codex_stderr_log_mode", "all"))
        normalized_reasoning_effort = str(reasoning_effort or settings.codex_reasoning_effort).strip().lower()
        if normalized_reasoning_effort not in {"low", "medium", "high"}:
            normalized_reasoning_effort = settings.codex_reasoning_effort
        normalized_resume_session_id = str(resume_session_id or "").strip()
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
                        settings.codex_model,
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
                    settings.codex_model,
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
                if "login" in stderr.lower() or "auth" in stderr.lower():
                    auth_url = _extract_first_url(stderr)
                    auth_link = f" Open this link to authenticate: {auth_url}." if auth_url else ""
                    message = (
                        "Codex CLI is not authenticated."
                        f"{auth_link} "
                        "Run `docker compose run --rm worker codex login --device-auth`."
                    )
                    raise CodexRuntimeError(message)
                raise CodexRuntimeError(
                    f"Codex CLI command failed (exit={returncode}): {stderr or 'no stderr'}"
                )
            output_file.seek(0)
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
        model=settings.codex_model,
        max_output_tokens=settings.codex_max_output_tokens,
        command=codex_command,
        _request=_request,
    )
