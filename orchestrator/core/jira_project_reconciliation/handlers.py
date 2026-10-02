from __future__ import annotations

from orchestrator.core.jira_project_reconciliation.dependencies import (
    JiraProjectReconciliationHandlerDeps,
)
from orchestrator.core.jira_project_reconciliation.service import (
    JiraProjectReconciliationStepFailed,
    JiraProjectReconciliationWorkflowService,
    build_default_jira_project_reconciliation_gateway,
)
from orchestrator.core.workflow.runtime import WorkflowAdvanceOutcome
from orchestrator.storage.models import Project, Tenant


class JiraProjectReconciliationAdvanceHandler:
    def __init__(self, *, deps: JiraProjectReconciliationHandlerDeps) -> None:
        self._deps = deps

    def advance(
        self,
        *,
        session,
        settings,  # noqa: ANN001
        workflow_type,
        request,
        lifecycle,
    ) -> WorkflowAdvanceOutcome:
        request_id = str(request.payload.get("request_id") or "").strip()
        if not request_id:
            raise RuntimeError("Jira project reconciliation requires request_id")
        project_id = str(request.project_id or "").strip()
        if not project_id:
            raise RuntimeError("Jira project reconciliation requires project_id")
        tenant = session.get(Tenant, request.tenant_id)
        if tenant is None:
            raise RuntimeError(f"Tenant {request.tenant_id} was not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant.tenant_id:
            raise RuntimeError(
                f"Project {project_id} was not found for tenant {tenant.tenant_id}"
            )
        if bool(getattr(project, "is_archived", False)):
            raise RuntimeError(
                "Jira project reconciliation cannot run for an archived project"
            )
        jira_project_key = (
            str(getattr(project, "jira_project_key", "") or "").strip().upper()
        )
        if not jira_project_key:
            raise RuntimeError(
                "Jira project reconciliation requires a project Jira key"
            )

        lifecycle.ensure_execution(
            display_name=f"Jira reconciliation {jira_project_key}",
            description=f"Reconcile Jira project {jira_project_key} into Master Builder parent planning workflows.",
        )
        max_items = max(1, int(request.payload.get("max_items") or 1000))
        gateway_factory = (
            self._deps.gateway_factory
            or build_default_jira_project_reconciliation_gateway
        )
        gateway = gateway_factory(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            max_items=max_items,
        )
        service = JiraProjectReconciliationWorkflowService(
            session=session,
            settings=settings,
            lifecycle=lifecycle,
            workflow_type=workflow_type,
            tenant=tenant,
            project=project,
            gateway=gateway,
            max_items=max_items,
            request_id=request_id,
        )
        try:
            result = service.run()
        except JiraProjectReconciliationStepFailed as exc:
            return WorkflowAdvanceOutcome(
                handled=True,
                failed=True,
                reason=str(exc),
                extra={
                    "workflow_id": service.workflow.workflow_id,
                    "execution_id": service.workflow.execution_id,
                },
            )
        return WorkflowAdvanceOutcome(
            handled=True,
            extra={
                "workflow_id": service.workflow.workflow_id,
                "execution_id": service.workflow.execution_id,
                "summary": result.summary.to_payload(),
            },
        )
