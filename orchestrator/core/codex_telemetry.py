from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)


def build_codex_log_sink(
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
        raise ValueError("Codex telemetry requires non-empty channel")
    if not normalized_tenant:
        raise ValueError("Codex telemetry requires non-empty tenant_id")
    if not normalized_command:
        raise ValueError("Codex telemetry requires non-empty command")

    logger.info(
        "codex_telemetry_start channel=%s tenant_id=%s project_id=%s command=%s issue_key=%s working_dir=%s",
        normalized_channel,
        normalized_tenant,
        normalized_project,
        normalized_command,
        normalized_issue_key,
        normalized_working_dir,
    )

    def _sink(stream: str, message: str) -> None:
        logger.info(
            "codex_telemetry_line channel=%s tenant_id=%s project_id=%s command=%s issue_key=%s stream=%s message=%s",
            normalized_channel,
            normalized_tenant,
            normalized_project,
            normalized_command,
            normalized_issue_key,
            str(stream or "").strip().lower() or "stdout",
            str(message or "").strip(),
        )

    return _sink
