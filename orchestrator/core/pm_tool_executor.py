from __future__ import annotations

from typing import Any

from orchestrator.core.pm_plugin_catalog import tool_catalog_payload
from orchestrator.tools.stitch_tool import maybe_invoke_stitch_tool_for_stage_plan


def _allowed_tool_names() -> set[str]:
    return {str(item.get("tool_name") or "").strip() for item in tool_catalog_payload()}


def execute_pm_tool_calls(
    *,
    tool_calls: list[dict[str, Any]] | None,
    tenant_id: str,
    project_id: str | None,
    default_prompt: str,
) -> list[dict[str, Any]]:
    """Execute allowlisted PM tool calls and return artifact rows.

    Unknown tools or malformed arguments are ignored.
    """
    outputs: list[dict[str, Any]] = []
    if not isinstance(tool_calls, list) or not tool_calls:
        return outputs

    allowed = _allowed_tool_names()
    for raw in tool_calls:
        if not isinstance(raw, dict):
            continue
        tool = str(raw.get("tool") or "").strip()
        if not tool or tool not in allowed:
            continue
        args = raw.get("arguments")
        arguments = args if isinstance(args, dict) else {}

        if tool == "stitch.synthesize_screen":
            prompt = str(arguments.get("prompt") or "").strip() or str(default_prompt or "").strip()
            if not prompt:
                continue
            artifact = maybe_invoke_stitch_tool_for_stage_plan(
                brief=prompt,
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if artifact:
                outputs.append(dict(artifact))
    return outputs

