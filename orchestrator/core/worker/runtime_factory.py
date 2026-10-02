from __future__ import annotations

from sqlalchemy.orm import Session

from orchestrator.core.runtime.tools import execute_agent_tool
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.runtime.runtime import build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.qa.demo_service import planned_capture_target_constraints_payload
from orchestrator.core.worker.capabilities import resolve_worker_capability_context
from orchestrator.core.workflow.orchestrated_run_runner import (
    OrchestratedRunWorkflowExecutor,
)
from orchestrator.core.workflow.runner import WorkflowRunner
import json


def _workflow_runtime_settings(settings):
    normalized_sandbox_mode = (
        str(getattr(settings, "codex_sandbox_mode", "") or "").strip().lower()
    )
    capability_context = resolve_worker_capability_context(
        raw_value=getattr(settings, "worker_capabilities", None),
        source="Settings.worker_capabilities",
    )
    if (
        normalized_sandbox_mode == "workspace-write"
        and "macos" in capability_context.available_values
    ):
        return settings.model_copy(update={"codex_sandbox_mode": "danger-full-access"})
    return settings


def build_workflow_runner_for_session(*, session: Session) -> WorkflowRunner:
    settings = get_settings()
    effective_settings = _workflow_runtime_settings(settings)
    runtime = build_codex_runtime(session=session, settings=effective_settings)
    agents = OrchestratedRunWorkflowExecutor(
        runtime=runtime,
        pm_capture_target_constraints_json=json.dumps(
            planned_capture_target_constraints_payload(settings=effective_settings)
        ),
        runtime_resolver=lambda stage, request: build_runtime_for_selector(
            session=session,
            settings=effective_settings,
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            selector=f"workflow.{stage}",
            agent_role=(
                "engineering"
                if stage == "dev"
                else (
                    "test"
                    if stage == "test"
                    else ("review" if stage == "review" else None)
                )
            ),
            agent_name=(
                "workflow_dev_default"
                if stage == "dev"
                else (
                    "workflow_test_default"
                    if stage == "test"
                    else ("workflow_review_default" if stage == "review" else None)
                )
            ),
        ),
        execute_tool=lambda tenant_id, project_id, run_id, issue_key, stage, tool_name, tool_args, worker_platform: (
            execute_agent_tool(
                session=session,
                settings=settings,
                tenant_id=tenant_id,
                project_id=project_id,
                run_id=run_id,
                issue_key=issue_key,
                stage=stage,
                tool_name=tool_name,
                tool_args=tool_args,
                worker_platform=worker_platform,
            )
        ),
    )
    return WorkflowRunner(agents)
