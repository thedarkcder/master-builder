from __future__ import annotations

from orchestrator.core.workflow.runtime import WorkflowAdvanceOutcome
from orchestrator.storage.models import Project, Tenant

DEMO_PROOF_HANDLER_KEY = "demo_proof"
DEMO_PROOF_STEP_PREVIEW_LEASE = "preview_lease"


def _required_payload_string(payload: dict[str, object], field_name: str) -> str:
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise RuntimeError(f"Demo proof workflow requires {field_name}")
    return value


class DemoProofWorkflowAdvanceHandler:
    def advance(
        self,
        *,
        session,
        settings,  # noqa: ANN001
        workflow_type,
        request,
        lifecycle,
    ) -> WorkflowAdvanceOutcome:
        _ = settings
        request_id = _required_payload_string(request.payload, "request_id")
        proof_scope_id = _required_payload_string(request.payload, "proof_scope_id")
        commit_sha = _required_payload_string(request.payload, "commit_sha")
        project_id = str(request.project_id or "").strip()
        if not project_id:
            raise RuntimeError("Demo proof workflow requires project_id")
        tenant = session.get(Tenant, request.tenant_id)
        if tenant is None:
            raise RuntimeError(f"Tenant {request.tenant_id} was not found")
        project = session.get(Project, project_id)
        if project is None or project.tenant_id != tenant.tenant_id:
            raise RuntimeError(f"Project {project_id} was not found for tenant {tenant.tenant_id}")
        if bool(getattr(project, "is_archived", False)):
            raise RuntimeError("Demo proof workflow cannot run for an archived project")

        run_id = str(request.payload.get("run_id") or "").strip() or None
        pr_url = str(request.payload.get("pr_url") or "").strip() or None
        required_capture_targets = request.payload.get("required_capture_targets")
        if not isinstance(required_capture_targets, list) or not required_capture_targets:
            required_capture_targets = ["browser", "ios", "android"]

        lifecycle.ensure_execution(
            display_name=f"Demo proof {proof_scope_id}",
            description={
                "proof_scope_id": proof_scope_id,
                "commit_sha": commit_sha,
                "run_id": run_id,
                "pr_url": pr_url,
                "required_capture_targets": [str(target) for target in required_capture_targets],
                "request_id": request_id,
            },
        )
        operation, attempt = lifecycle.start_operation_attempt(
            operation_type=DEMO_PROOF_STEP_PREVIEW_LEASE,
            run_id=run_id,
            idempotency_key=f"demo-proof:{proof_scope_id}:preview-lease",
            target_system="master_builder",
            target_ref=proof_scope_id,
            summary=f"Acquire preview lease for demo proof scope {proof_scope_id}.",
        )
        lifecycle.wait_started_operation(
            operation=operation,
            attempt=attempt,
            summary=f"Waiting for preview lease acquisition for demo proof scope {proof_scope_id}.",
        )
        return WorkflowAdvanceOutcome(
            handled=True,
            reason="preview_lease_requested",
            extra={
                "workflow_id": lifecycle.workflow.workflow_id,
                "execution_id": lifecycle.workflow.execution_id,
                "proof_scope_id": proof_scope_id,
            },
        )
