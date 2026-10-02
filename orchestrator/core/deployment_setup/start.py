from dataclasses import dataclass
from typing import Literal
from uuid import uuid4

from orchestrator.core.config import Settings
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    workflow_execution_id,
)
from orchestrator.core.workflow.runtime import (
    WorkflowAdvanceRequest,
    WorkflowTrigger,
    build_workflow_runtime,
)

PROJECT_DEPLOYMENT_SETUP_WORKFLOW_TYPE_KEY = "project_deployment_setup"


@dataclass(frozen=True)
class ProjectDeploymentSetupStartResult:
    workflow_id: str
    status: Literal["started"] = "started"


def project_deployment_setup_execution_key(*, tenant_id: str, project_id: str) -> str:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    if not normalized_tenant_id:
        raise ValueError("Deployment setup workflow requires tenant_id")
    if not normalized_project_id:
        raise ValueError("Deployment setup workflow requires project_id")
    return uuid4().hex


def project_deployment_setup_workflow_id(*, tenant_id: str, project_id: str) -> str:
    return workflow_execution_id(
        workflow_type_key=PROJECT_DEPLOYMENT_SETUP_WORKFLOW_TYPE_KEY,
        execution_key=project_deployment_setup_execution_key(
            tenant_id=tenant_id, project_id=project_id
        ),
    )


def start_project_deployment_setup_workflow(
    *,
    session,
    settings: Settings,
    tenant_id: str,
    project_id: str,
    project_name: str,
    execution_key: str,
    production_branch: str,
    requested_by_user_id: str | None,
) -> ProjectDeploymentSetupStartResult:
    normalized_execution_key = str(execution_key or "").strip()
    if not normalized_execution_key:
        raise ValueError("Deployment setup workflow requires execution_key")
    normalized_workflow_id = workflow_execution_id(
        workflow_type_key=PROJECT_DEPLOYMENT_SETUP_WORKFLOW_TYPE_KEY,
        execution_key=normalized_execution_key,
    )
    if not normalized_workflow_id:
        raise ValueError("Deployment setup workflow requires workflow_id")
    runtime = build_workflow_runtime(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
    )
    result = runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key=PROJECT_DEPLOYMENT_SETUP_WORKFLOW_TYPE_KEY,
            tenant_id=tenant_id,
            tenant=None,
            project_id=project_id,
            execution=WorkflowExecutionReference(
                key=normalized_execution_key,
                source=WorkflowSourceReference(
                    source_system="deployment_setup",
                    source_ref=project_id,
                    external_id=normalized_workflow_id,
                    display_name=f"Deployment setup for {project_name}",
                    description={
                        "project_id": project_id,
                        "production_branch": production_branch,
                    },
                    attributes={
                        "requested_by_user_id": requested_by_user_id,
                        "production_branch": production_branch,
                    },
                ),
            ),
            payload={
                "requested_by_user_id": requested_by_user_id,
                "production_branch": production_branch,
            },
            trigger=WorkflowTrigger(event="deployment_setup_completed"),
        )
    )
    if not result.handled:
        raise RuntimeError(result.reason or "Deployment setup workflow was not started")

    return ProjectDeploymentSetupStartResult(workflow_id=normalized_workflow_id)
