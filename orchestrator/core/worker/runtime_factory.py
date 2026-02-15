from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.core.codex_agents import CodexWorkflowAgents
from orchestrator.core.codex_runtime import build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.run_logs import record_run_log_event
from orchestrator.core.workflow.runner import WorkflowRunner
from orchestrator.storage.db import create_session_factory


def build_workflow_runner_for_session(*, session: Session) -> WorkflowRunner:
    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    session_factory = create_session_factory(database_url=settings.database_url)

    def _log_sink(payload: dict) -> None:
        with session_factory() as log_session:
            record_run_log_event(
                session=log_session,
                tenant_id=str(payload.get("tenant_id") or ""),
                project_id=str(payload.get("project_id") or "") or None,
                run_id=str(payload.get("run_id") or ""),
                issue_key=str(payload.get("issue_key") or "") or None,
                agent_id=settings.agent_id,
                stage=str(payload.get("stage") or "unknown"),
                attempt=int(payload["attempt"]) if payload.get("attempt") is not None else None,
                stream=str(payload.get("stream") or "stdout"),
                message=str(payload.get("message") or ""),
            )
            log_session.commit()

    agents = CodexWorkflowAgents(runtime=runtime, log_sink=_log_sink)
    return WorkflowRunner(agents)
