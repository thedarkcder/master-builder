from __future__ import annotations

from dataclasses import dataclass

from orchestrator.temporal.workflows.development_team_run import DevelopmentTeamRunWorkflow
from orchestrator.temporal.workflows.handler_backed_workflow import HandlerBackedWorkflow
from orchestrator.temporal.workflows.project_deployment_setup import ProjectDeploymentSetupWorkflow


@dataclass(frozen=True)
class TemporalWorkflowBinding:
    handler_key: str
    workflow_name: str
    workflow_defn: object
    execution_mode: str = "run"
    task_queue: str = "master-builder"
    workflow_execution_timeout_seconds: int = 86400
    workflow_run_timeout_seconds: int = 86400
    activity_start_to_close_timeout_seconds: int = 7200
    human_input_resume_timeout_seconds: int = 7200


_TEMPORAL_BINDINGS = (
    TemporalWorkflowBinding(
        handler_key="development_team_run",
        workflow_name="DevelopmentTeamRunWorkflow",
        workflow_defn=DevelopmentTeamRunWorkflow,
    ),
    TemporalWorkflowBinding(
        handler_key="jira_project_reconciliation",
        workflow_name="HandlerBackedWorkflow",
        workflow_defn=HandlerBackedWorkflow,
        execution_mode="handler",
    ),
    TemporalWorkflowBinding(
        handler_key="jira_parent_feature",
        workflow_name="HandlerBackedWorkflow",
        workflow_defn=HandlerBackedWorkflow,
        execution_mode="handler",
    ),
    TemporalWorkflowBinding(
        handler_key="project_deployment_setup",
        workflow_name="ProjectDeploymentSetupWorkflow",
        workflow_defn=ProjectDeploymentSetupWorkflow,
        execution_mode="setup",
    ),
    TemporalWorkflowBinding(
        handler_key="demo_proof",
        workflow_name="HandlerBackedWorkflow",
        workflow_defn=HandlerBackedWorkflow,
        execution_mode="handler",
    ),
    TemporalWorkflowBinding(
        handler_key="pr_remediation",
        workflow_name="DevelopmentTeamRunWorkflow",
        workflow_defn=DevelopmentTeamRunWorkflow,
        execution_mode="run",
    ),
)

_TEMPORAL_WORKFLOW_REGISTRY = {binding.workflow_name: binding.workflow_defn for binding in _TEMPORAL_BINDINGS}
_TEMPORAL_BINDINGS_BY_HANDLER_KEY = {binding.handler_key: binding for binding in _TEMPORAL_BINDINGS}


def list_registered_temporal_workflow_names() -> list[str]:
    return sorted(_TEMPORAL_WORKFLOW_REGISTRY)


def resolve_temporal_workflow_definition(*, workflow_name: str):
    normalized = str(workflow_name or "").strip()
    workflow_defn = _TEMPORAL_WORKFLOW_REGISTRY.get(normalized)
    if workflow_defn is None:
        raise LookupError(f"Temporal workflow definition is not registered: {workflow_name}")
    return workflow_defn


def resolve_temporal_binding_for_handler(*, handler_key: str) -> TemporalWorkflowBinding:
    binding = _TEMPORAL_BINDINGS_BY_HANDLER_KEY.get(str(handler_key or "").strip())
    if binding is None:
        raise LookupError(f"Temporal engine is not available for handler: {handler_key}")
    return binding
