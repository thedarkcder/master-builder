from __future__ import annotations

from orchestrator.core.workflow.definition import (
    WorkflowStepKind,
    WorkflowWorkUnitKind,
    WorkflowWorkUnitRetryPolicy,
    workflow_step,
    workflow_work_unit,
)

DEPLOYMENT_SETUP_STEP_ANALYZE = "repo_deployment_analysis"
DEPLOYMENT_SETUP_STEP_PREPARE = "deployment_configuration"
DEPLOYMENT_SETUP_STEP_RELEASE = "initial_release"

DEPLOYMENT_SETUP_WU_CHECKOUT = "repo_deployment_analysis.checkout"
DEPLOYMENT_SETUP_WU_ANALYZE = "repo_deployment_analysis.analyze"
DEPLOYMENT_SETUP_WU_PREPARE_APPS = "deployment_configuration.prepare_apps"
DEPLOYMENT_SETUP_WU_RELEASE = "initial_release.create"

_EXTERNAL_RETRY = WorkflowWorkUnitRetryPolicy(
    max_attempts=3,
    initial_interval_seconds=30,
    max_interval_seconds=300,
    backoff_coefficient=2.0,
)


class ProjectDeploymentSetupWorkflowDefinition:
    @workflow_work_unit(
        key=DEPLOYMENT_SETUP_WU_CHECKOUT,
        step_key=DEPLOYMENT_SETUP_STEP_ANALYZE,
        label="Prepare repository checkout",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_EXTERNAL_RETRY,
    )
    @workflow_work_unit(
        key=DEPLOYMENT_SETUP_WU_ANALYZE,
        step_key=DEPLOYMENT_SETUP_STEP_ANALYZE,
        label="Analyze deployable units",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=_EXTERNAL_RETRY,
    )
    @workflow_step(
        key=DEPLOYMENT_SETUP_STEP_ANALYZE,
        label="Repository deployment analysis",
        kind=WorkflowStepKind.INTEGRATION,
        retryable=True,
        description="Analyze the selected branch and discover deployable services.",
    )
    def repo_deployment_analysis(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key=DEPLOYMENT_SETUP_WU_PREPARE_APPS,
        step_key=DEPLOYMENT_SETUP_STEP_PREPARE,
        label="Prepare deployment configuration",
        kind=WorkflowWorkUnitKind.ASSEMBLY,
    )
    @workflow_step(
        key=DEPLOYMENT_SETUP_STEP_PREPARE,
        label="Deployment configuration",
        kind=WorkflowStepKind.BUSINESS,
        after=DEPLOYMENT_SETUP_STEP_ANALYZE,
        retryable=True,
        description="Persist deployable units and branch-scoped deployment configuration.",
    )
    def deployment_configuration(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key=DEPLOYMENT_SETUP_WU_RELEASE,
        step_key=DEPLOYMENT_SETUP_STEP_RELEASE,
        label="Create initial release",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=_EXTERNAL_RETRY,
    )
    @workflow_step(
        key=DEPLOYMENT_SETUP_STEP_RELEASE,
        label="Initial release",
        kind=WorkflowStepKind.SIDE_EFFECT,
        after=DEPLOYMENT_SETUP_STEP_PREPARE,
        retryable=True,
        description="Create the first deployment release from the selected branch commit.",
    )
    def initial_release(self) -> None:
        raise NotImplementedError
