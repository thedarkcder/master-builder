from __future__ import annotations

from orchestrator.temporal.workflows.development_team_run import DevelopmentTeamRunWorkflow

_TEMPORAL_WORKFLOW_REGISTRY = {
    "DevelopmentTeamRunWorkflow": DevelopmentTeamRunWorkflow,
}


def list_registered_temporal_workflow_names() -> list[str]:
    return sorted(_TEMPORAL_WORKFLOW_REGISTRY)


def resolve_temporal_workflow_definition(*, workflow_name: str):
    normalized = str(workflow_name or "").strip()
    workflow_defn = _TEMPORAL_WORKFLOW_REGISTRY.get(normalized)
    if workflow_defn is None:
        raise LookupError(f"Temporal workflow definition is not registered: {workflow_name}")
    return workflow_defn
