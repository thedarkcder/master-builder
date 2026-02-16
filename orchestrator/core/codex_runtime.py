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

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class CodexRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexRuntime:
    model: str
    max_output_tokens: int
    command: str
    _request: Callable[[str, str, str | None, Callable[[str, str], None] | None], str]

    def run_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        working_dir: str | None = None,
        on_log_line: Callable[[str, str], None] | None = None,
    ) -> str:
        output = self._request(system_prompt, user_prompt, working_dir, on_log_line).strip()
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
    ) -> dict:
        output = self.run_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            working_dir=working_dir,
            on_log_line=on_log_line,
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
            raise CodexRuntimeError(f"Invalid JSON payload from Codex runtime: {exc}") from exc

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
        ) -> str:
            _ = on_log_line
            _ = working_dir
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
    ) -> str:
        combined_prompt = (
            "You are the Codex orchestration runtime. "
            "Follow the system instructions exactly and return only the required output.\n\n"
            f"## System instructions\n{system_prompt}\n\n"
            f"## User request\n{user_prompt}"
        )
        command_cwd = str(working_dir).strip() if working_dir else None
        stderr_log_mode = _normalize_stderr_log_mode(getattr(settings, "codex_stderr_log_mode", "all"))
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", suffix=".txt") as output_file:
            process = subprocess.Popen(  # noqa: S603
                [
                    codex_command,
                    "exec",
                    "--skip-git-repo-check",
                    "--sandbox",
                    settings.codex_sandbox_mode,
                    "-c",
                    f'reasoning.effort="{settings.codex_reasoning_effort}"',
                    "--color",
                    "never",
                    "--model",
                    settings.codex_model,
                    "--output-last-message",
                    output_file.name,
                    "-",
                ],
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
                for line in iter(pipe.readline, ""):
                    collector.append(line)
                    _mark_activity()
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
                    raise CodexRuntimeError(
                        "Codex CLI is not authenticated. "
                        "Run `docker compose run --rm worker codex login --device-auth`."
                    )
                raise CodexRuntimeError(
                    f"Codex CLI command failed (exit={returncode}): {stderr or 'no stderr'}"
                )
            output_file.seek(0)
            output = output_file.read().strip()
            if output:
                return output
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
