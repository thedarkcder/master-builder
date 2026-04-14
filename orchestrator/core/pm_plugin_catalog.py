from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PmPluginCatalogEntry:
    plugin_id: str
    task: str
    description: str
    prompt_selector: str
    allowed_tools: tuple[str, ...]
    expected_outputs: tuple[str, ...]


@dataclass(frozen=True)
class PmToolCatalogEntry:
    tool_name: str
    description: str
    required_args: tuple[str, ...]
    optional_args: tuple[str, ...]


def list_pm_plugins() -> list[PmPluginCatalogEntry]:
    return [
        PmPluginCatalogEntry(
            plugin_id="design",
            task="design_planning",
            description="Interview stakeholders, synthesize design direction, and track approvals.",
            prompt_selector="workflow.design_planning",
            allowed_tools=("stitch.synthesize_screen",),
            expected_outputs=("design_brief", "design_direction", "open_questions"),
        )
    ]


def list_pm_tools() -> list[PmToolCatalogEntry]:
    return [
        PmToolCatalogEntry(
            tool_name="stitch.synthesize_screen",
            description="Generate a design screen artifact from a design brief prompt.",
            required_args=("prompt",),
            optional_args=("project_title", "device_type"),
        )
    ]


def plugin_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "plugin_id": item.plugin_id,
            "task": item.task,
            "description": item.description,
            "prompt_selector": item.prompt_selector,
            "allowed_tools": list(item.allowed_tools),
            "expected_outputs": list(item.expected_outputs),
        }
        for item in list_pm_plugins()
    ]


def tool_catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "tool_name": item.tool_name,
            "description": item.description,
            "required_args": list(item.required_args),
            "optional_args": list(item.optional_args),
        }
        for item in list_pm_tools()
    ]

