from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.core.codex_runtime import build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.workflow.orchestrated_run_runner import OrchestratedRunWorkflowExecutor
from orchestrator.core.workflow.runner import WorkflowRunner


def build_workflow_runner_for_session(*, session: Session) -> WorkflowRunner:
    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    agents = OrchestratedRunWorkflowExecutor(runtime=runtime)
    return WorkflowRunner(agents)
