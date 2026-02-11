from __future__ import annotations

import json
import traceback

from orchestrator.core.guardrails import redact_sensitive_text


def emit_hard_error(
    *,
    event: str,
    error_ref: str,
    exc: Exception,
    context: dict[str, str] | None = None,
) -> None:
    """Best-effort fallback diagnostics that bypass logger handlers/filters.

    This writes a compact JSON envelope and traceback directly to stderr so
    production failures remain visible even if logger sinks are misconfigured.
    """

    payload: dict[str, object] = {
        "event": event,
        "error_ref": error_ref,
    }
    if context:
        payload["context"] = context
    print(redact_sensitive_text(f"HARD_ERROR {json.dumps(payload, sort_keys=True)}"), flush=True)
    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(redact_sensitive_text(traceback_text), flush=True)
