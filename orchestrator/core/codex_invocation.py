from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.codex_telemetry import build_codex_log_sink


@dataclass(frozen=True)
class CodexInvocationContext:
    channel: str
    tenant_id: str
    project_id: str | None
    command: str
    stage: str
    working_dir: str
    issue_key: str | None = None
    run_id: str | None = None
    attempt: int | None = None


def invoke_codex_json(
    *,
    runtime: CodexRuntime,
    context: CodexInvocationContext,
    system_prompt: str,
    user_prompt: str,
    extra_on_log_line: Callable[[str, str], None] | None = None,
) -> dict:
    return runtime.run_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        working_dir=context.working_dir,
        on_log_line=_combined_log_sink(context=context, extra_on_log_line=extra_on_log_line),
    )


def _combined_log_sink(
    *,
    context: CodexInvocationContext,
    extra_on_log_line: Callable[[str, str], None] | None,
) -> Callable[[str, str], None]:
    telemetry_sink = build_codex_log_sink(
        channel=context.channel,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        command=f"{context.command}.{context.stage}",
        issue_key=context.issue_key,
        working_dir=context.working_dir,
    )

    def _sink(stream: str, message: str) -> None:
        telemetry_sink(stream, message)
        if extra_on_log_line is not None:
            extra_on_log_line(stream, message)

    return _sink
