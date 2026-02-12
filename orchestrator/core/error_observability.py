from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone

from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.observability import current_log_context


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

    log_context = current_log_context()
    payload: dict[str, object] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "ERROR",
        "environment": "runtime-fallback",
        "platform_version": "runtime-fallback",
        "tenant_id": (context or {}).get("tenant_id", log_context.get("tenant_id")),
        "project_id": (context or {}).get("project_id", log_context.get("project_id")),
        "agent_id": (context or {}).get("agent_id", log_context.get("agent_id")),
        "correlation_id": (context or {}).get("correlation_id", log_context.get("correlation_id")),
        "event_type": event,
        "message": f"HARD_ERROR ref={error_ref}",
        "metadata": {
            "error_ref": error_ref,
            "context": context or {},
        },
    }
    print(redact_sensitive_text(json.dumps(payload, sort_keys=True)), flush=True)
    traceback_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(redact_sensitive_text(traceback_text), flush=True)
