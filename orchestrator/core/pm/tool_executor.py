from __future__ import annotations

from typing import Any

from orchestrator.core.pm.plugin_catalog import tool_catalog_payload
from orchestrator.core.runtime.payload_models import PMToolCallPayload
from orchestrator.tools.stitch_tool import maybe_invoke_stitch_tool_for_stage_plan


def _allowed_tool_names() -> set[str]:
    return {str(item.get("tool_name") or "").strip() for item in tool_catalog_payload()}


def execute_pm_tool_calls(
    *,
    tool_calls: list[PMToolCallPayload],
    tenant_id: str,
    project_id: str | None,
) -> list[dict[str, Any]]:
    """Execute allowlisted typed PM tool calls and return artifact rows."""
    outputs: list[dict[str, Any]] = []
    if not tool_calls:
        return outputs

    allowed = _allowed_tool_names()
    for call in tool_calls:
        tool = call.tool.strip()
        if tool not in allowed:
            raise RuntimeError(f"PM tool call uses unknown tool '{tool}'")
        arguments = call.arguments

        if tool == "stitch.synthesize_screen":
            prompt = str(arguments.get("prompt") or "").strip()
            if not prompt:
                raise RuntimeError("PM tool call stitch.synthesize_screen missing prompt")
            artifact = maybe_invoke_stitch_tool_for_stage_plan(
                brief=prompt,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if artifact:
                outputs.append(dict(artifact))
    return outputs
