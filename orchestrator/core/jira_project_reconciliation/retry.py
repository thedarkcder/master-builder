from __future__ import annotations

from orchestrator.core.jira_project_reconciliation.dependencies import (
    JiraProjectReconciliationHandlerDeps,
)
from orchestrator.core.jira_project_reconciliation.service import (
    JiraProjectReconciliationWorkflowService,
    build_default_jira_project_reconciliation_gateway,
    latest_reconciliation_request_id_for_workflow,
)
from orchestrator.core.workflow.advance import DurableWorkflowLifecycle
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
)
from orchestrator.core.jira_project_reconciliation.workflow import (
    JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
    JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
    JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
    JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
    JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
)
from orchestrator.core.workflow.advance import (
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
    WorkflowOperationRetryCapability,
    WorkflowOperationRetryRequest,
)
from orchestrator.core.workflow.definition import WorkflowDefinition
from orchestrator.storage.models import Project, Tenant


class JiraProjectReconciliationOperationRetryHandler:
    def __init__(self, *, deps: JiraProjectReconciliationHandlerDeps) -> None:
        self._deps = deps
        self._retryable_steps = (
            JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
            JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
            JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
            JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
            JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
        )

    @classmethod
    def declared_operation_retry_capabilities(
        cls,
        workflow_type: WorkflowDefinition,
    ) -> tuple[WorkflowOperationRetryCapability, ...]:
        return tuple(
            WorkflowOperationRetryCapability(operation_type=step_key)
            for step_key in (
                JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
                JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
                JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
                JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
                JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
            )
            if workflow_type.has_step(step_key)
            and workflow_type.step(step_key).retryable
        )

    def operation_retry_capabilities(
        self, workflow_type
    ) -> tuple[WorkflowOperationRetryCapability, ...]:  # noqa: ANN001
        return self.declared_operation_retry_capabilities(workflow_type)

    def retry_operation(
        self,
        *,
        request: WorkflowOperationRetryRequest,
    ):
        operation_type = str(request.operation.operation_type or "").strip()
        if operation_type not in self._retryable_steps:
            raise UnsupportedWorkflowOperationRetryError(
                f"Jira project reconciliation operation {operation_type} is not retryable."
            )
        tenant = request.session.get(Tenant, request.workflow.tenant_id)
        if tenant is None:
            raise InvalidWorkflowOperationRetryError(
                f"Tenant {request.workflow.tenant_id} was not found"
            )
        project_id = str(request.workflow.project_id or "").strip()
        if not project_id:
            raise InvalidWorkflowOperationRetryError(
                "Workflow is not bound to a project"
            )
        project = request.session.get(Project, project_id)
        if project is None or project.tenant_id != tenant.tenant_id:
            raise InvalidWorkflowOperationRetryError(
                f"Project {project_id} was not found for tenant {tenant.tenant_id}"
            )
        if not str(getattr(project, "jira_project_key", "") or "").strip():
            raise InvalidWorkflowOperationRetryError(
                "Project Jira key is required for reconciliation retry"
            )
        gateway_factory = (
            self._deps.gateway_factory
            or build_default_jira_project_reconciliation_gateway
        )
        gateway = gateway_factory(
            session=request.session,
            settings=request.settings,
            tenant=tenant,
            project=project,
            max_items=1000,
        )
        lifecycle = DurableWorkflowLifecycle(
            session=request.session,
            workflow_type=request.workflow_type,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            execution=WorkflowExecutionReference(
                key=project.project_id,
                source=WorkflowSourceReference(
                    source_system=str(request.workflow.source_system or "").strip(),
                    source_ref=str(request.workflow.source_ref or "").strip(),
                    external_id=str(
                        getattr(request.workflow, "source_external_id", "") or ""
                    ).strip()
                    or None,
                    display_name=getattr(request.workflow, "display_name", None),
                    description=getattr(request.workflow, "source_description", None),
                ),
            ),
        )
        service = JiraProjectReconciliationWorkflowService(
            session=request.session,
            settings=request.settings,
            lifecycle=lifecycle,
            workflow_type=request.workflow_type,
            tenant=tenant,
            project=project,
            gateway=gateway,
            max_items=1000,
            request_id=latest_reconciliation_request_id_for_workflow(
                session=request.session,
                workflow_id=request.workflow.workflow_id,
                project_id=project.project_id,
            ),
        )
        return service.retry_operation(operation_type=operation_type)
