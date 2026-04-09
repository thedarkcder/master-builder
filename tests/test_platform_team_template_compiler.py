from __future__ import annotations

import pytest

from orchestrator.core.platform_team_template_compiler import compile_platform_team_template


def _base_payload() -> dict:
    return {
        "team_key": "marketing_launch",
        "label": "Marketing Launch",
        "roles": [
            {
                "role_key": "strategist",
                "label": "Strategist",
                "position": 1,
                "persona_key": "launch_strategist",
                "agent_key": "launch_strategy_agent",
            }
        ],
        "tasks": [
            {
                "task_key": "brief",
                "label": "Brief",
                "owner_role_key": "strategist",
                "position": 1,
                "executor_kind": None,
                "artifact_contract": {},
                "approval_rule": {},
            }
        ],
        "edges": [],
    }


def test_compile_platform_team_template_rejects_empty_tasks() -> None:
    payload = _base_payload()
    payload["tasks"] = []
    with pytest.raises(ValueError, match="at least one task"):
        compile_platform_team_template(payload)


def test_compile_platform_team_template_rejects_cyclic_dependencies() -> None:
    payload = _base_payload()
    payload["tasks"] = [
        {
            "task_key": "brief",
            "label": "Brief",
            "owner_role_key": "strategist",
            "position": 1,
            "executor_kind": None,
            "artifact_contract": {},
            "approval_rule": {},
        },
        {
            "task_key": "copy_draft",
            "label": "Copy Draft",
            "owner_role_key": "strategist",
            "position": 2,
            "executor_kind": None,
            "artifact_contract": {},
            "approval_rule": {},
        },
    ]
    payload["edges"] = [
        {"from_task_key": "brief", "to_task_key": "copy_draft"},
        {"from_task_key": "copy_draft", "to_task_key": "brief"},
    ]
    with pytest.raises(ValueError, match="acyclic"):
        compile_platform_team_template(payload)


def test_compile_platform_team_template_rejects_duplicate_edges() -> None:
    payload = _base_payload()
    payload["tasks"] = [
        {
            "task_key": "brief",
            "label": "Brief",
            "owner_role_key": "strategist",
            "position": 1,
            "executor_kind": None,
            "artifact_contract": {},
            "approval_rule": {},
        },
        {
            "task_key": "copy_draft",
            "label": "Copy Draft",
            "owner_role_key": "strategist",
            "position": 2,
            "executor_kind": None,
            "artifact_contract": {},
            "approval_rule": {},
        },
    ]
    payload["edges"] = [
        {"from_task_key": "brief", "to_task_key": "copy_draft"},
        {"from_task_key": "brief", "to_task_key": "copy_draft"},
    ]
    with pytest.raises(ValueError, match="Duplicate edge"):
        compile_platform_team_template(payload)
