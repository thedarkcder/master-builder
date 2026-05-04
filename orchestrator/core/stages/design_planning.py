"""LLM-backed design stage planning using ``workflow.design_planning`` prompts."""

from __future__ import annotations

import json
from typing import Any

from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.pm.plugin_catalog import plugin_catalog_payload, tool_catalog_payload
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime.payload_models import DesignPlanningPayload
from orchestrator.core.runtime.invocation import AgentInvocationContext, invoke_runtime_json


def invoke_stage_design_planning_llm(
    *,
    session,
    settings: Any,
    tenant_id: str,
    project_id: str | None,
    working_dir: str,
    issue_key: str | None,
    stage_plugin: str,
    stage_status: str,
    stakeholder_text: str,
    assistant_summary: str,
    stage_artifacts: dict[str, Any],
    stage_open_questions: list[str],
    stage_tool_outputs: list[dict[str, Any]],
) -> DesignPlanningPayload:
    runtime = build_runtime_for_selector(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
        selector="workflow.design_planning",
    )
    context = AgentInvocationContext(
        channel="discord",
        tenant_id=tenant_id,
        project_id=project_id,
        command="pm",
        stage="design_planning",
        working_dir=working_dir,
        issue_key=issue_key,
        reasoning_effort="medium",
    )
    payload = invoke_runtime_json(
        runtime=runtime,
        context=context,
        system_prompt=render_prompt("workflow/design_planning_system.j2"),
        user_prompt=render_prompt(
            "workflow/design_planning_user.j2",
            stage_plugin=stage_plugin,
            stage_status=stage_status,
            stakeholder_text=stakeholder_text,
            assistant_summary=assistant_summary,
            stage_artifacts_json=json.dumps(stage_artifacts, ensure_ascii=False, sort_keys=True),
            stage_open_questions_json=json.dumps(stage_open_questions, ensure_ascii=False, sort_keys=True),
            stage_tool_outputs_json=json.dumps(stage_tool_outputs, ensure_ascii=False, sort_keys=True),
            plugin_catalog_json=json.dumps(plugin_catalog_payload(), ensure_ascii=False, sort_keys=True),
            tool_catalog_json=json.dumps(tool_catalog_payload(), ensure_ascii=False, sort_keys=True),
        ),
    )
    try:
        return DesignPlanningPayload.from_payload(payload)
    except RuntimeError as exc:
        raise CodexRuntimeError(str(exc)) from exc
