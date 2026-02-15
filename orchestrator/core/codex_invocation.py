from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Callable
from uuid import uuid4

from orchestrator.core.config import get_settings
from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.codex_telemetry import build_codex_log_sink
from orchestrator.core.run_logs import record_run_log_event
from orchestrator.storage.db import create_session_factory

logger = logging.getLogger(__name__)


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
    invocation_id: str | None = None


def invoke_codex_json(
    *,
    runtime: CodexRuntime,
    context: CodexInvocationContext,
    system_prompt: str,
    user_prompt: str,
    extra_on_log_line: Callable[[str, str], None] | None = None,
) -> dict:
    invocation_id = context.invocation_id or uuid4().hex
    return runtime.run_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        working_dir=context.working_dir,
        on_log_line=_combined_log_sink(
            context=CodexInvocationContext(
                channel=context.channel,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                command=context.command,
                stage=context.stage,
                working_dir=context.working_dir,
                issue_key=context.issue_key,
                run_id=context.run_id,
                attempt=context.attempt,
                invocation_id=invocation_id,
            ),
            extra_on_log_line=extra_on_log_line,
        ),
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
        try:
            _persist_codex_log_line(context=context, stream=stream, message=message)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "codex_log_persist_failed tenant_id=%s run_id=%s channel=%s command=%s stage=%s error=%s",
                context.tenant_id,
                context.run_id,
                context.channel,
                context.command,
                context.stage,
                exc,
            )
        if extra_on_log_line is not None:
            extra_on_log_line(stream, message)

    return _sink


def _persist_codex_log_line(*, context: CodexInvocationContext, stream: str, message: str) -> None:
    settings = get_settings()
    session_factory = create_session_factory(database_url=settings.database_url)
    with session_factory() as session:
        record_run_log_event(
            session=session,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            run_id=context.run_id,
            issue_key=context.issue_key,
            agent_id=settings.agent_id,
            invocation_id=str(context.invocation_id or "").strip(),
            channel=context.channel,
            command=f"{context.command}.{context.stage}",
            working_dir=context.working_dir,
            stage=context.stage,
            attempt=context.attempt,
            stream=stream,
            message=message,
        )
        session.commit()
