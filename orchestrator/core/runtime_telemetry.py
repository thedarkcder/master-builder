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
    workflow_id: str | None = None,
    operation_id: str | None = None,
    run_id: str | None = None,
    attempt: int | None = None,
    issue_key: str | None = None,
) -> Callable[[str, str], None]:
    normalized_channel = str(channel or "").strip()
    normalized_tenant = str(tenant_id or "").strip()
    normalized_project = str(project_id or "").strip() or None
    normalized_command = str(command or "").strip()
    normalized_working_dir = str(working_dir or "").strip() or None
    normalized_workflow_id = str(workflow_id or "").strip() or None
    normalized_operation_id = str(operation_id or "").strip() or None
    normalized_run_id = str(run_id or "").strip() or None
    normalized_issue_key = str(issue_key or "").strip() or None
    if not normalized_channel:
        raise ValueError("Runtime telemetry requires non-empty channel")
    if not normalized_tenant:
        raise ValueError("Runtime telemetry requires non-empty tenant_id")
    if not normalized_command:
        raise ValueError("Runtime telemetry requires non-empty command")

    logger.info(
        "Runtime telemetry started.",
        extra={
            "event_type": "runtime_log_stream_started",
            "tenant_id": normalized_tenant,
            "project_id": normalized_project,
            "metadata": {
                "channel": normalized_channel,
                "command": normalized_command,
                "workflow_id": normalized_workflow_id,
                "operation_id": normalized_operation_id,
                "run_id": normalized_run_id,
                "attempt": attempt,
                "issue_key": normalized_issue_key,
                "working_dir": normalized_working_dir,
                "phase": "start",
            },
        },
    )

    def _sink(stream: str, message: str) -> None:
        redacted_message = redact_sensitive_text(str(message or "").strip())
        logger.info(
            redacted_message or "runtime log line",
            extra={
                "event_type": "runtime_log",
                "tenant_id": normalized_tenant,
                "project_id": normalized_project,
                "metadata": {
                    "channel": normalized_channel,
                    "command": normalized_command,
                    "workflow_id": normalized_workflow_id,
                    "operation_id": normalized_operation_id,
                    "run_id": normalized_run_id,
                    "attempt": attempt,
                    "issue_key": normalized_issue_key,
                    "stream": str(stream or "").strip().lower() or "stdout",
                    "message": redacted_message,
                    "phase": "line",
                },
            },
        )

    return _sink
