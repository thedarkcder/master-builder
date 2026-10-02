from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.core.parent_feature_workflow.dependencies import (
    ParentFeatureWorkflowHandlerDeps,
)
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_PARENT_UPDATE,
)
from orchestrator.core.parent_feature_workflow.retry_handlers import (
    BacklogPlanningRetryExecutor,
    JiraChildFanoutRetryExecutor,
    JiraParentUpdateRetryExecutor,
)
from orchestrator.core.parent_feature_workflow.retry_support import (
    ParentWorkflowRetryContext,
)
from orchestrator.core.workflow.advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
    WorkflowOperationRetryCapability,
    WorkflowOperationRetryRequest,
)
from orchestrator.core.workflow.definition import WorkflowDefinition
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.storage.models import Project, Tenant


class _ParentFeatureRetryExecutor(Protocol):
    operation_type: str

    def execute(
        self, *, context: ParentWorkflowRetryContext
    ) -> WorkflowOperationHandle: ...


@dataclass(frozen=True)
class _ParentFeatureRetryOperationCapability:
    operation_type: str
    executor: _ParentFeatureRetryExecutor


def _validate_parent_workflow_operation_retry(
    *,
    workflow_type: WorkflowDefinition,
    operation_type: str,
) -> None:
    normalized = str(operation_type or "").strip()
    try:
        step = workflow_type.step(normalized)
    except LookupError as exc:
        raise UnsupportedWorkflowOperationRetryError(
            f"Workflow {workflow_type.workflow_type_key} has no retryable operation {normalized or '<missing>'}."
        ) from exc
    if not step.retryable:
        raise UnsupportedWorkflowOperationRetryError(
            f"Workflow operation {normalized} is not retryable in workflow {workflow_type.workflow_type_key}."
        )


class ParentFeatureWorkflowOperationRetryHandler:
    def __init__(
        self,
        *,
        deps: ParentFeatureWorkflowHandlerDeps,
    ) -> None:
        self._retry_capabilities = {
            capability.operation_type: capability
            for capability in (
                _ParentFeatureRetryOperationCapability(
                    operation_type=PARENT_OP_JIRA_PARENT_UPDATE,
                    executor=JiraParentUpdateRetryExecutor(deps=deps),
                ),
                _ParentFeatureRetryOperationCapability(
                    operation_type=PARENT_OP_BACKLOG_PLANNING,
                    executor=BacklogPlanningRetryExecutor(deps=deps),
                ),
                _ParentFeatureRetryOperationCapability(
                    operation_type=PARENT_OP_JIRA_CHILD_FANOUT,
                    executor=JiraChildFanoutRetryExecutor(deps=deps),
                ),
            )
        }

    @classmethod
    def declared_operation_retry_capabilities(
        cls,
        workflow_type: WorkflowDefinition,
    ) -> tuple[WorkflowOperationRetryCapability, ...]:
        capabilities: list[WorkflowOperationRetryCapability] = []
        for operation_type in (
            PARENT_OP_JIRA_PARENT_UPDATE,
            PARENT_OP_BACKLOG_PLANNING,
            PARENT_OP_JIRA_CHILD_FANOUT,
        ):
            if (
                workflow_type.has_step(operation_type)
                and workflow_type.step(operation_type).retryable
            ):
                capabilities.append(
                    WorkflowOperationRetryCapability(operation_type=operation_type)
                )
        return tuple(capabilities)

    def operation_retry_capabilities(
        self, workflow_type
    ) -> tuple[WorkflowOperationRetryCapability, ...]:  # noqa: ANN001
        return self.declared_operation_retry_capabilities(workflow_type)

    def retry_operation(
        self,
        *,
        request: WorkflowOperationRetryRequest,
    ) -> WorkflowOperationHandle:
        operation_type = str(request.operation.operation_type or "").strip()
        _validate_parent_workflow_operation_retry(
            workflow_type=request.workflow_type,
            operation_type=operation_type,
        )
        tenant = request.session.get(Tenant, request.workflow.tenant_id)
        if tenant is None:
            raise InvalidWorkflowOperationRetryError(
                f"Tenant {request.workflow.tenant_id} was not found"
            )
        if not request.workflow.project_id:
            raise InvalidWorkflowOperationRetryError(
                "Workflow is not bound to a project"
            )
        project = request.session.get(Project, request.workflow.project_id)
        if project is None:
            raise InvalidWorkflowOperationRetryError(
                f"Project {request.workflow.project_id} was not found"
            )
        if not str(project.jira_project_key or "").strip():
            raise InvalidWorkflowOperationRetryError(
                "Project Jira key is required for parent workflow operations"
            )
        context = ParentWorkflowRetryContext(
            session=request.session,
            settings=request.settings,
            workflow_type=request.workflow_type,
            workflow=request.workflow,
            operation=request.operation,
            tenant=tenant,
            project=project,
        )
        capability = self._retry_capability(operation_type=operation_type)
        return capability.executor.execute(context=context)

    def _retry_capability(
        self, *, operation_type: str
    ) -> _ParentFeatureRetryOperationCapability:
        capability = self._retry_capabilities.get(operation_type)
        if capability is None:
            raise UnsupportedWorkflowOperationRetryError(
                f"Parent feature workflow operation {operation_type} is retryable but has no retry implementation."
            )
        return capability
