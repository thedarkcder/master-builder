from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.config import Settings
from orchestrator.core.secret_manager import resolve_secret_ref

_JSON_BLOCK_PATTERN = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


class CodexRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexRuntime:
    model: str
    timeout_seconds: int
    max_output_tokens: int
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
    session: Session,
    settings: Settings,
    request_override: Callable[[str, str], str] | None = None,
) -> CodexRuntime:
    if request_override is not None:
        return CodexRuntime(
            model=settings.codex_model,
            timeout_seconds=settings.codex_timeout_seconds,
            max_output_tokens=settings.codex_max_output_tokens,
            _request=request_override,
        )

    secret_ref = settings.codex_api_key_secret_ref.strip()
    if not secret_ref:
        raise CodexRuntimeError("Codex API key secret ref is not configured")

    api_key = (
        resolve_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        )
        or ""
    ).strip()
    if not api_key:
        raise CodexRuntimeError(f"Missing Codex API key for secret ref '{secret_ref}'")

    try:
        from openai import OpenAI
    except ModuleNotFoundError as exc:  # pragma: no cover - import safety for packaging
        raise CodexRuntimeError(
            "OpenAI SDK is not installed in this environment (missing dependency 'openai')"
        ) from exc

    client = OpenAI(api_key=api_key, timeout=float(settings.codex_timeout_seconds))

    def _request(system_prompt: str, user_prompt: str) -> str:
        response = client.responses.create(
            model=settings.codex_model,
            max_output_tokens=settings.codex_max_output_tokens,
            input=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return (response.output_text or "").strip()

    return CodexRuntime(
        model=settings.codex_model,
        timeout_seconds=settings.codex_timeout_seconds,
        max_output_tokens=settings.codex_max_output_tokens,
        _request=_request,
    )
