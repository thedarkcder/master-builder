from __future__ import annotations

import logging
from collections.abc import Callable

from orchestrator.core.guardrails import redact_sensitive_text

logger = logging.getLogger(__name__)


def build_runtime_log_sink(
    *,
    channel: str,
    tenant_id: str,
    project_id: str | None,
    command: str,
    working_dir: str | None,
    issue_key: str | None = None,
) -> Callable[[str, str], None]:
    normalized_channel = str(channel or "").strip()
    normalized_tenant = str(tenant_id or "").strip()
    normalized_project = str(project_id or "").strip() or None
    normalized_command = str(command or "").strip()
    normalized_working_dir = str(working_dir or "").strip() or None
    normalized_issue_key = str(issue_key or "").strip() or None
    if not normalized_channel:
        raise ValueError("Runtime telemetry requires non-empty channel")
    if not normalized_tenant:
        raise ValueError("Runtime telemetry requires non-empty tenant_id")
    if not normalized_command:
        raise ValueError("Runtime telemetry requires non-empty command")

    logger.info(
        "runtime_telemetry_start",
        extra={
            "event_type": "orchestrator.core.runtime_telemetry",
            "tenant_id": normalized_tenant,
            "project_id": normalized_project,
            "metadata": {
                "channel": normalized_channel,
                "command": normalized_command,
                "issue_key": normalized_issue_key,
                "working_dir": normalized_working_dir,
                "phase": "start",
            },
        },
    )

    def _sink(stream: str, message: str) -> None:
        logger.info(
            "runtime_telemetry_line",
            extra={
                "event_type": "orchestrator.core.runtime_telemetry",
                "tenant_id": normalized_tenant,
                "project_id": normalized_project,
                "metadata": {
                    "channel": normalized_channel,
                    "command": normalized_command,
                    "issue_key": normalized_issue_key,
                    "stream": str(stream or "").strip().lower() or "stdout",
                    "message": redact_sensitive_text(str(message or "").strip()),
                    "phase": "line",
                },
            },
        )

    return _sink
