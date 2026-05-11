from __future__ import annotations

from orchestrator.core.workflow.definition import (
    WorkflowDefinition,
    WorkflowDefinitionRegistry,
    WorkflowRetryPolicyDefinition,
    WorkflowStepKind,
    WorkflowWorkUnitIdempotencyPolicy,
    WorkflowWorkUnitKind,
    WorkflowWorkUnitRetryPolicy,
    infer_workflow_steps,
    infer_workflow_work_units,
    workflow_step,
    workflow_work_unit,
)

ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION = "run_attempt_execution"
ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME = "human_input_resume"
ISSUE_EXECUTION_STEP_JIRA_COMMENT_PROJECTION = "jira_comment_projection"
ISSUE_EXECUTION_STEP_DISCORD_FOLLOWUP_PROJECTION = "discord_followup_projection"
ISSUE_EXECUTION_STEP_NOTIFICATION_EMIT = "notification_emit"
PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION = "run_attempt_execution"
PR_REMEDIATION_STEP_HUMAN_INPUT_RESUME = "human_input_resume"
PR_REMEDIATION_STEP_NOTIFICATION_EMIT = "notification_emit"


class IssueExecutionWorkflow:
    @workflow_work_unit(
        key="run_attempt_execution.prepare",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Prepare run",
        kind=WorkflowWorkUnitKind.PURE_COMPUTE,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_work_unit(
        key="run_attempt_execution.repo_setup",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Prepare repository",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key="run_attempt_execution.runtime_invocation",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Runtime invocation",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key="run_attempt_execution.finalize",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Finalize run",
        kind=WorkflowWorkUnitKind.ASSEMBLY,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Run attempt execution",
        kind=WorkflowStepKind.BUSINESS,
        description="Execute the claimed development run.",
    )
    def run_attempt_execution(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="human_input_resume.resume",
        step_key=ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
        label="Resume human input",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
        label="Human input resume",
        kind=WorkflowStepKind.HUMAN_GATE,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Resume a run after required human input is supplied.",
    )
    def human_input_resume(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="jira_comment_projection.api",
        step_key=ISSUE_EXECUTION_STEP_JIRA_COMMENT_PROJECTION,
        label="Publish Jira comment",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_JIRA_COMMENT_PROJECTION,
        label="Jira comment projection",
        kind=WorkflowStepKind.SIDE_EFFECT,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Publish run state back to Jira when needed.",
    )
    def jira_comment_projection(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="discord_followup_projection.api",
        step_key=ISSUE_EXECUTION_STEP_DISCORD_FOLLOWUP_PROJECTION,
        label="Publish Discord follow-up",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_DISCORD_FOLLOWUP_PROJECTION,
        label="Discord follow-up projection",
        kind=WorkflowStepKind.NOTIFICATION,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Publish run follow-up state to Discord when needed.",
    )
    def discord_followup_projection(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="notification_emit.emit",
        step_key=ISSUE_EXECUTION_STEP_NOTIFICATION_EMIT,
        label="Emit notification",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_NOTIFICATION_EMIT,
        label="Notification emit",
        kind=WorkflowStepKind.NOTIFICATION,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Emit user/admin notifications for run state.",
    )
    def notification_emit(self) -> None:
        raise NotImplementedError


class PrRemediationWorkflow:
    @workflow_work_unit(
        key="run_attempt_execution.pr_context_load",
        step_key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Load PR context",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key="run_attempt_execution.runtime_invocation",
        step_key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Runtime invocation",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_work_unit(
        key="run_attempt_execution.github_update",
        step_key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Publish GitHub update",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_step(
        key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Run attempt execution",
        kind=WorkflowStepKind.BUSINESS,
        description="Execute the claimed PR remediation run.",
    )
    def run_attempt_execution(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="human_input_resume.resume",
        step_key=PR_REMEDIATION_STEP_HUMAN_INPUT_RESUME,
        label="Resume human input",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_step(
        key=PR_REMEDIATION_STEP_HUMAN_INPUT_RESUME,
        label="Human input resume",
        kind=WorkflowStepKind.HUMAN_GATE,
        after=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Resume PR remediation after required human input is supplied.",
    )
    def human_input_resume(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="notification_emit.emit",
        step_key=PR_REMEDIATION_STEP_NOTIFICATION_EMIT,
        label="Emit notification",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300),
    )
    @workflow_step(
        key=PR_REMEDIATION_STEP_NOTIFICATION_EMIT,
        label="Notification emit",
        kind=WorkflowStepKind.NOTIFICATION,
        after=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
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
    from orchestrator.core.deployment_setup.workflow import ProjectDeploymentSetupWorkflowDefinition
    from orchestrator.core.jira_project_reconciliation.workflow import JiraProjectReconciliationWorkflow
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
            work_units=infer_workflow_work_units(IssueExecutionWorkflow),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="jira_project_reconciliation",
            system_key="jira_project_reconciliation",
            handler_key="jira_project_reconciliation",
            label="Jira project reconciliation",
            description="Durable Jira project scan, label reconciliation, and parent workflow queuing.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=3,
                initial_interval_seconds=30,
                max_interval_seconds=300,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(JiraProjectReconciliationWorkflow),
            work_units=infer_workflow_work_units(JiraProjectReconciliationWorkflow),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="project_deployment_setup",
            system_key="project_deployment_setup",
            handler_key="project_deployment_setup",
            label="Project deployment setup",
            description="Durable project deployment setup, deployable-unit discovery, and initial release.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=3,
                initial_interval_seconds=30,
                max_interval_seconds=300,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(ProjectDeploymentSetupWorkflowDefinition),
            work_units=infer_workflow_work_units(ProjectDeploymentSetupWorkflowDefinition),
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
            work_units=infer_workflow_work_units(ParentFeaturePlanningWorkflow),
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
            work_units=infer_workflow_work_units(PrRemediationWorkflow),
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
