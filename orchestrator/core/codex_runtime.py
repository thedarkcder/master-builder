from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from orchestrator.core.config import Settings

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class CodexRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexRuntime:
    model: str
    timeout_seconds: int
    max_output_tokens: int
    command: str
    _request: Callable[[str, str], str]

    def run_text(self, *, system_prompt: str, user_prompt: str) -> str:
        output = self._request(system_prompt, user_prompt).strip()
        if not output:
            raise CodexRuntimeError("Codex runtime returned an empty response")
        return output

    def run_json(self, *, system_prompt: str, user_prompt: str) -> dict:
        output = self.run_text(system_prompt=system_prompt, user_prompt=user_prompt)
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



def build_codex_runtime(
    *,
    session: Session | None = None,
    settings: Settings,
    request_override: Callable[[str, str], str] | None = None,
) -> CodexRuntime:
    if request_override is not None:
        return CodexRuntime(
            model=settings.codex_model,
            timeout_seconds=settings.codex_timeout_seconds,
            max_output_tokens=settings.codex_max_output_tokens,
            command="override",
            _request=request_override,
        )

    codex_command = (settings.codex_cli_command or "").strip()
    if not codex_command:
        raise CodexRuntimeError("Codex CLI command is not configured")
    if shutil.which(codex_command) is None:
        raise CodexRuntimeError(
            f"Codex CLI command '{codex_command}' was not found in PATH"
        )

    def _request(system_prompt: str, user_prompt: str) -> str:
        combined_prompt = (
            "You are the Codex orchestration runtime. "
            "Follow the system instructions exactly and return only the required output.\n\n"
            f"## System instructions\n{system_prompt}\n\n"
            f"## User request\n{user_prompt}"
        )
        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", suffix=".txt") as output_file:
            process = subprocess.run(
                [
                    codex_command,
                    "exec",
                    "--skip-git-repo-check",
                    "--color",
                    "never",
                    "--model",
                    settings.codex_model,
                    "--output-last-message",
                    output_file.name,
                    "-",
                ],
                input=combined_prompt,
                text=True,
                capture_output=True,
                timeout=float(settings.codex_timeout_seconds),
                check=False,
            )
            if process.returncode != 0:
                stderr = (process.stderr or "").strip()
                if "login" in stderr.lower() or "auth" in stderr.lower():
                    raise CodexRuntimeError(
                        "Codex CLI is not authenticated. "
                        "Run `docker compose run --rm worker codex login --device-auth`."
                    )
                raise CodexRuntimeError(
                    f"Codex CLI command failed (exit={process.returncode}): {stderr or 'no stderr'}"
                )
            output_file.seek(0)
            output = output_file.read().strip()
            if output:
                return output
            stdout = (process.stdout or "").strip()
            if stdout:
                return stdout
            raise CodexRuntimeError("Codex CLI returned empty output")

    return CodexRuntime(
        model=settings.codex_model,
        timeout_seconds=settings.codex_timeout_seconds,
        max_output_tokens=settings.codex_max_output_tokens,
        command=codex_command,
        _request=_request,
    )
