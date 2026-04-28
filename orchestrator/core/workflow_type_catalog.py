from __future__ import annotations

from orchestrator.core.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionRegistry,
    WorkflowRetryPolicyDefinition,
    WorkflowStepKind,
    infer_workflow_steps,
    workflow_step,
)


class IssueExecutionWorkflow:
    @workflow_step(
        key="run_attempt_execution",
        label="Run attempt execution",
        kind=WorkflowStepKind.BUSINESS,
        retryable=True,
        description="Execute the claimed development run.",
    )
    def run_attempt_execution(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key="human_input_resume",
        label="Human input resume",
        kind=WorkflowStepKind.HUMAN_GATE,
        after="run_attempt_execution",
        required=False,
        description="Resume a run after required human input is supplied.",
    )
    def human_input_resume(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key="jira_comment_projection",
        label="Jira comment projection",
        kind=WorkflowStepKind.SIDE_EFFECT,
        after="run_attempt_execution",
        required=False,
        description="Publish run state back to Jira when needed.",
    )
    def jira_comment_projection(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key="discord_followup_projection",
        label="Discord follow-up projection",
        kind=WorkflowStepKind.NOTIFICATION,
        after="run_attempt_execution",
        required=False,
        description="Publish run follow-up state to Discord when needed.",
    )
    def discord_followup_projection(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key="notification_emit",
        label="Notification emit",
        kind=WorkflowStepKind.NOTIFICATION,
        after="run_attempt_execution",
        required=False,
        description="Emit user/admin notifications for run state.",
    )
    def notification_emit(self) -> None:
        raise NotImplementedError


class PrRemediationWorkflow:
    @workflow_step(
        key="run_attempt_execution",
        label="Run attempt execution",
        kind=WorkflowStepKind.BUSINESS,
        retryable=True,
        description="Execute the claimed PR remediation run.",
    )
    def run_attempt_execution(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key="human_input_resume",
        label="Human input resume",
        kind=WorkflowStepKind.HUMAN_GATE,
        after="run_attempt_execution",
        required=False,
        description="Resume PR remediation after required human input is supplied.",
    )
    def human_input_resume(self) -> None:
        raise NotImplementedError

    @workflow_step(
        key="notification_emit",
        label="Notification emit",
        kind=WorkflowStepKind.NOTIFICATION,
        after="run_attempt_execution",
        required=False,
        description="Emit user/admin notifications for PR remediation state.",
    )
    def notification_emit(self) -> None:
        raise NotImplementedError


def normalize_workflow_retry_policy_config(raw: dict | None) -> dict[str, object]:
    config = raw if isinstance(raw, dict) else {}
    return {
        "manual_retry_enabled": bool(config.get("manual_retry_enabled", True)),
        "max_attempts": max(1, int(config.get("max_attempts") or 1)),
        "initial_interval_seconds": max(0, int(config.get("initial_interval_seconds") or 0)),
        "max_interval_seconds": max(0, int(config.get("max_interval_seconds") or 0)),
        "backoff_coefficient": max(1.0, float(config.get("backoff_coefficient") or 1.0)),
    }


workflow_definition_registry = WorkflowDefinitionRegistry()
_builtin_workflows_registered = False


def _register_builtin_workflows() -> None:
    global _builtin_workflows_registered
    if _builtin_workflows_registered:
        return
    from orchestrator.core.parent_feature_workflow.planning import ParentFeaturePlanningWorkflow

    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="issue_execution",
            system_key="issue_execution",
            handler_key="development_team_run",
            label="Issue execution",
            description="Durable execution for an engineering issue run.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=5,
                initial_interval_seconds=30,
                max_interval_seconds=900,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(IssueExecutionWorkflow),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="parent_planning",
            system_key="parent_planning",
            handler_key="jira_parent_feature",
            label="Parent planning",
            description="Durable parent feature planning and engineering child fanout.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=3,
                initial_interval_seconds=30,
                max_interval_seconds=300,
                backoff_coefficient=2.0,
            ),
            capabilities={"child_issue_links": True},
            steps=infer_workflow_steps(ParentFeaturePlanningWorkflow),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="pr_remediation",
            system_key="pr_remediation",
            handler_key="pr_remediation",
            label="PR remediation",
            description="Durable execution for PR remediation runs.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=5,
                initial_interval_seconds=30,
                max_interval_seconds=900,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(PrRemediationWorkflow),
        )
    )
    _builtin_workflows_registered = True


def _ensure_builtin_workflows_registered() -> None:
    _register_builtin_workflows()


def get_workflow_type(_session=None, *, workflow_type_key: str) -> WorkflowDefinition:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.get(workflow_type_key)


def get_workflow_type_by_system_key(_session=None, *, system_key: str) -> WorkflowDefinition:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.get_by_system_key(system_key)


def get_workflow_type_by_handler_key(_session=None, *, handler_key: str) -> WorkflowDefinition:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.get_by_handler(handler_key)


def list_workflow_types(_session=None) -> tuple[WorkflowDefinition, ...]:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.list()


def validate_workflow_operation_type(*, workflow_type_key: str, operation_type: str):
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.validate_operation_type(
        workflow_type_key=workflow_type_key,
        operation_type=operation_type,
    )


def validate_persisted_workflow_definitions(*, session) -> None:  # noqa: ANN001
    from sqlalchemy import select

    from orchestrator.storage.models import WorkflowExecution, WorkflowOperation

    active_workflows = session.execute(
        select(WorkflowExecution).where(WorkflowExecution.status.in_(("queued", "running", "waiting_for_input", "failed")))
    ).scalars().all()
    for workflow in active_workflows:
        definition = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
        valid_step_keys = {step.key for step in definition.steps}
        operation_types = session.execute(
            select(WorkflowOperation.operation_type).where(WorkflowOperation.workflow_id == workflow.workflow_id)
        ).scalars().all()
        unknown = sorted(
            {
                str(operation_type or "").strip()
                for operation_type in operation_types
                if str(operation_type or "").strip() and str(operation_type or "").strip() not in valid_step_keys
            }
        )
        if unknown:
            raise RuntimeError(
                f"Workflow {workflow.workflow_id} references operation types not registered in code: {', '.join(unknown)}"
            )
