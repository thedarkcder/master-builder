from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass


ISSUE_WORKFLOW_TEAM_KEY = "issue_workflow"
ISSUE_WORKFLOW_TEAM_LABEL = "Engineering Workflow"
ISSUE_WORKFLOW_PM_EXECUTOR_KIND = "workflow.pm"
ISSUE_WORKFLOW_DEV_EXECUTOR_KIND = "workflow.dev"
ISSUE_WORKFLOW_TEST_EXECUTOR_KIND = "workflow.test"
ISSUE_WORKFLOW_REVIEW_EXECUTOR_KIND = "workflow.review"
ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND = {
    "pm": ISSUE_WORKFLOW_PM_EXECUTOR_KIND,
    "dev": ISSUE_WORKFLOW_DEV_EXECUTOR_KIND,
    "test": ISSUE_WORKFLOW_TEST_EXECUTOR_KIND,
    "review": ISSUE_WORKFLOW_REVIEW_EXECUTOR_KIND,
}


@dataclass(frozen=True)
class BuiltinIssuePersonaDefinition:
    persona_key: str
    label: str


@dataclass(frozen=True)
class BuiltinIssueAgentDefinition:
    agent_key: str
    label: str
    persona_key: str
    runtime_role_key: str
    named_agent_key: str
    selector_key: str
    default_profile_name: str


BUILTIN_ISSUE_PERSONAS = (
    BuiltinIssuePersonaDefinition(persona_key="pm", label="PM"),
    BuiltinIssuePersonaDefinition(persona_key="engineering", label="Engineering"),
    BuiltinIssuePersonaDefinition(persona_key="test", label="Test"),
    BuiltinIssuePersonaDefinition(persona_key="review", label="Review"),
)


BUILTIN_ISSUE_AGENTS = (
    BuiltinIssueAgentDefinition(
        agent_key="pm_primary",
        label="PM Primary",
        persona_key="pm",
        runtime_role_key="pm",
        named_agent_key="pm_primary",
        selector_key="workflow.pm",
        default_profile_name="general_planning_default",
    ),
    BuiltinIssueAgentDefinition(
        agent_key="workflow_dev_default",
        label="Workflow Dev",
        persona_key="engineering",
        runtime_role_key="engineering",
        named_agent_key="workflow_dev_default",
        selector_key="workflow.dev",
        default_profile_name="general_implementation_default",
    ),
    BuiltinIssueAgentDefinition(
        agent_key="workflow_test_default",
        label="Workflow Test",
        persona_key="test",
        runtime_role_key="test",
        named_agent_key="workflow_test_default",
        selector_key="workflow.test",
        default_profile_name="general_validation_default",
    ),
    BuiltinIssueAgentDefinition(
        agent_key="workflow_review_default",
        label="Workflow Review",
        persona_key="review",
        runtime_role_key="review",
        named_agent_key="workflow_review_default",
        selector_key="workflow.review",
        default_profile_name="general_review_default",
    ),
)


_ISSUE_WORKFLOW_TEMPLATE_PAYLOAD = {
    "roles": [
        {"role_key": "pm", "label": "PM", "position": 1, "persona_key": "pm", "agent_key": "pm_primary"},
        {"role_key": "engineering", "label": "DEV", "position": 2, "persona_key": "engineering", "agent_key": "workflow_dev_default"},
        {"role_key": "test", "label": "TEST", "position": 3, "persona_key": "test", "agent_key": "workflow_test_default"},
        {"role_key": "review", "label": "REVIEW", "position": 4, "persona_key": "review", "agent_key": "workflow_review_default"},
    ],
    "tasks": [
        {
            "task_key": "pm",
            "label": "PM",
            "owner_role_key": "pm",
            "position": 1,
            "executor_kind": ISSUE_WORKFLOW_PM_EXECUTOR_KIND,
            "artifact_contract": {"produces": ["pm_plan"]},
            "approval_rule": {},
        },
        {
            "task_key": "dev",
            "label": "DEV",
            "owner_role_key": "engineering",
            "position": 2,
            "executor_kind": ISSUE_WORKFLOW_DEV_EXECUTOR_KIND,
            "artifact_contract": {"produces": ["dev_result"]},
            "approval_rule": {},
        },
        {
            "task_key": "test",
            "label": "TEST",
            "owner_role_key": "test",
            "position": 3,
            "executor_kind": ISSUE_WORKFLOW_TEST_EXECUTOR_KIND,
            "artifact_contract": {"produces": ["test_result"]},
            "approval_rule": {},
        },
        {
            "task_key": "review",
            "label": "REVIEW",
            "owner_role_key": "review",
            "position": 4,
            "executor_kind": ISSUE_WORKFLOW_REVIEW_EXECUTOR_KIND,
            "artifact_contract": {"produces": ["review_result"]},
            "approval_rule": {},
        },
    ],
    "edges": [
        {"from_task_key": "pm", "to_task_key": "dev"},
        {"from_task_key": "dev", "to_task_key": "test"},
        {"from_task_key": "test", "to_task_key": "review"},
    ],
}


def issue_workflow_template_payload() -> dict[str, object]:
    return deepcopy(_ISSUE_WORKFLOW_TEMPLATE_PAYLOAD)
