from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IssueWorkflowStageContract:
    stage_key: str
    task_key: str
    role_key: str
    label: str
    executor_kind: str
    artifact_type: str
    loop_stage: bool


ISSUE_WORKFLOW_TEAM_KEY = "issue_workflow"
ISSUE_WORKFLOW_TEAM_LABEL = "Engineering Workflow"

ISSUE_WORKFLOW_STAGE_CONTRACTS: tuple[IssueWorkflowStageContract, ...] = (
    IssueWorkflowStageContract(
        stage_key="pm",
        task_key="pm",
        role_key="pm",
        label="PM",
        executor_kind="workflow.pm",
        artifact_type="pm_plan",
        loop_stage=False,
    ),
    IssueWorkflowStageContract(
        stage_key="dev",
        task_key="dev",
        role_key="engineering",
        label="DEV",
        executor_kind="workflow.dev",
        artifact_type="dev_result",
        loop_stage=True,
    ),
    IssueWorkflowStageContract(
        stage_key="test",
        task_key="test",
        role_key="test",
        label="TEST",
        executor_kind="workflow.test",
        artifact_type="test_result",
        loop_stage=True,
    ),
    IssueWorkflowStageContract(
        stage_key="review",
        task_key="review",
        role_key="review",
        label="REVIEW",
        executor_kind="workflow.review",
        artifact_type="review_result",
        loop_stage=True,
    ),
)

ISSUE_WORKFLOW_STAGE_KEYS: tuple[str, ...] = tuple(stage.stage_key for stage in ISSUE_WORKFLOW_STAGE_CONTRACTS)
ISSUE_WORKFLOW_STAGE_TO_EXECUTOR_KIND: dict[str, str] = {
    stage.stage_key: stage.executor_kind for stage in ISSUE_WORKFLOW_STAGE_CONTRACTS
}
ISSUE_WORKFLOW_EXECUTOR_KIND_TO_STAGE: dict[str, str] = {
    stage.executor_kind: stage.stage_key for stage in ISSUE_WORKFLOW_STAGE_CONTRACTS
}
ISSUE_WORKFLOW_STAGE_TO_TASK_KEY: dict[str, str] = {
    stage.stage_key: stage.task_key for stage in ISSUE_WORKFLOW_STAGE_CONTRACTS
}
ISSUE_WORKFLOW_LOOP_STAGE_KEYS: tuple[str, ...] = tuple(
    stage.stage_key for stage in ISSUE_WORKFLOW_STAGE_CONTRACTS if stage.loop_stage
)


def issue_workflow_stage_for_executor_kind(executor_kind: str) -> str | None:
    normalized = str(executor_kind or "").strip().lower()
    if not normalized:
        return None
    return ISSUE_WORKFLOW_EXECUTOR_KIND_TO_STAGE.get(normalized)


def issue_workflow_task_key_for_stage(stage_key: str) -> str | None:
    normalized = str(stage_key or "").strip().lower()
    if not normalized:
        return None
    return ISSUE_WORKFLOW_STAGE_TO_TASK_KEY.get(normalized)


def is_issue_workflow_executor_kind(executor_kind: str) -> bool:
    return issue_workflow_stage_for_executor_kind(executor_kind) is not None
