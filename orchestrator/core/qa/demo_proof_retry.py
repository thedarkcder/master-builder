from __future__ import annotations

import json

from orchestrator.core.workflow.advance import (
    DurableWorkflowLifecycle,
    InvalidWorkflowOperationRetryError,
    UnsupportedWorkflowOperationRetryError,
    WorkflowOperationRetryCapability,
    WorkflowOperationRetryRequest,
)
from orchestrator.core.workflow.definition import WorkflowDefinition
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.core.workflow.type_catalog import (
    DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
    DEMO_PROOF_STEP_PREVIEW_CLEANUP,
    DEMO_PROOF_STEP_PREVIEW_LEASE,
    DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
    DEMO_PROOF_STEP_RECORDING,
    DEMO_PROOF_STEP_RELEASE,
)
from orchestrator.core.workflow.work_units import seed_declared_work_units_for_operation_attempt
from orchestrator.core.qa.demo_proof_handlers import demo_proof_operation_input_payload
from orchestrator.storage.models import Project, Tenant


_DEMO_PROOF_RETRYABLE_STEPS = (
    DEMO_PROOF_STEP_PREVIEW_LEASE,
    DEMO_PROOF_STEP_RELEASE,
    DEMO_PROOF_STEP_RECORDING,
    DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
    DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
    DEMO_PROOF_STEP_PREVIEW_CLEANUP,
)


def _validate_retryable_demo_proof_operation(
    *,
    workflow_type: WorkflowDefinition,
    operation_type: str,
) -> None:
    normalized = str(operation_type or "").strip()
    if normalized not in _DEMO_PROOF_RETRYABLE_STEPS:
        raise UnsupportedWorkflowOperationRetryError(
            f"Demo proof operation {normalized or '<missing>'} is not retryable."
        )
    if not workflow_type.has_step(normalized) or not workflow_type.step(normalized).retryable:
        raise UnsupportedWorkflowOperationRetryError(
            f"Workflow operation {normalized} is not retryable in workflow {workflow_type.workflow_type_key}."
        )


def _description_payload(raw_value: object) -> dict[str, object]:
    normalized = str(raw_value or "").strip()
    if not normalized:
        return {}
    try:
        decoded = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise InvalidWorkflowOperationRetryError("Demo proof workflow description is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise InvalidWorkflowOperationRetryError("Demo proof workflow description must be a JSON object")
    return decoded


def _proof_scope_id(*, request: WorkflowOperationRetryRequest) -> str:
    target_ref = str(getattr(request.operation, "target_ref", "") or "").strip()
    if target_ref:
        return target_ref
    description = _description_payload(getattr(request.workflow, "source_description", None))
    proof_scope_id = str(description.get("proof_scope_id") or "").strip()
    if proof_scope_id:
        return proof_scope_id
    raise InvalidWorkflowOperationRetryError("Demo proof operation retry requires proof_scope_id")


def _run_id(*, request: WorkflowOperationRetryRequest) -> str | None:
    operation_run_id = str(getattr(request.operation, "run_id", "") or "").strip()
    if operation_run_id:
        return operation_run_id
    description = _description_payload(getattr(request.workflow, "source_description", None))
    return str(description.get("run_id") or "").strip() or None


class DemoProofWorkflowOperationRetryHandler:
    @classmethod
    def declared_operation_retry_capabilities(
        cls,
        workflow_type: WorkflowDefinition,
    ) -> tuple[WorkflowOperationRetryCapability, ...]:
        return tuple(
            WorkflowOperationRetryCapability(operation_type=operation_type)
            for operation_type in _DEMO_PROOF_RETRYABLE_STEPS
            if workflow_type.has_step(operation_type) and workflow_type.step(operation_type).retryable
        )

    def operation_retry_capabilities(self, workflow_type) -> tuple[WorkflowOperationRetryCapability, ...]:  # noqa: ANN001
        return self.declared_operation_retry_capabilities(workflow_type)

    def retry_operation(
        self,
        *,
        request: WorkflowOperationRetryRequest,
    ) -> WorkflowOperationHandle:
        operation_type = str(getattr(request.operation, "operation_type", "") or "").strip()
        _validate_retryable_demo_proof_operation(
            workflow_type=request.workflow_type,
            operation_type=operation_type,
        )
        tenant = request.session.get(Tenant, request.workflow.tenant_id)
        if tenant is None:
            raise InvalidWorkflowOperationRetryError(f"Tenant {request.workflow.tenant_id} was not found")
        project_id = str(getattr(request.workflow, "project_id", "") or "").strip()
        if not project_id:
            raise InvalidWorkflowOperationRetryError("Demo proof workflow is not bound to a project")
        project = request.session.get(Project, project_id)
        if project is None or project.tenant_id != tenant.tenant_id:
            raise InvalidWorkflowOperationRetryError(f"Project {project_id} was not found for tenant {tenant.tenant_id}")
        proof_scope_id = _proof_scope_id(request=request)
        description = _description_payload(getattr(request.workflow, "source_description", None))
        lifecycle = DurableWorkflowLifecycle(
            session=request.session,
            workflow_type=request.workflow_type,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            execution=WorkflowExecutionReference(
                key=str(getattr(request.workflow, "source_ref", "") or "").strip(),
                source=WorkflowSourceReference(
                    source_system=str(getattr(request.workflow, "source_system", "") or "").strip(),
                    source_ref=str(getattr(request.workflow, "source_ref", "") or "").strip(),
                    external_id=str(getattr(request.workflow, "source_external_id", "") or "").strip() or None,
                    display_name=getattr(request.workflow, "display_name", None),
                    description=getattr(request.workflow, "source_description", None),
                ),
            ),
        )
        operation, attempt = lifecycle.start_operation_attempt(
            operation_type=operation_type,
            run_id=_run_id(request=request),
            target_system=str(getattr(request.operation, "target_system", "") or "").strip() or "master_builder",
            target_ref=proof_scope_id,
            summary=f"Retry demo proof operation {operation_type} for proof scope {proof_scope_id}.",
        )
        seed_declared_work_units_for_operation_attempt(
            request.session,
            workflow_type=request.workflow_type,
            operation=operation,
            operation_attempt=attempt,
            input_payload=demo_proof_operation_input_payload(
                description=description,
                summary=f"Retry demo proof operation {operation_type} for proof scope {proof_scope_id}.",
            ),
        )
        lifecycle.wait_started_operation(
            operation=operation,
            attempt=attempt,
            summary=f"Waiting for retried demo proof operation {operation_type} for proof scope {proof_scope_id}.",
        )
        return WorkflowOperationHandle(
            operation_id=operation.operation_id,
            workflow_id=operation.workflow_id,
            operation_type=operation.operation_type,
            status=operation.status,
        )
