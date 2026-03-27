from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.core.agent_tools import execute_agent_tool
from orchestrator.core.codex_runtime import build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.workflow.orchestrated_run_runner import OrchestratedRunWorkflowExecutor
from orchestrator.core.workflow.runner import WorkflowRunner


def build_workflow_runner_for_session(*, session: Session) -> WorkflowRunner:
    settings = get_settings()
    runtime = build_codex_runtime(session=session, settings=settings)
    agents = OrchestratedRunWorkflowExecutor(
        runtime=runtime,
        execute_tool=lambda tenant_id, project_id, run_id, issue_key, stage, tool_name, tool_args: execute_agent_tool(
            session=session,
            settings=settings,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            issue_key=issue_key,
            stage=stage,
            tool_name=tool_name,
            tool_args=tool_args,
        ),
    )
    return WorkflowRunner(agents)
