from __future__ import annotations

from sqlalchemy import select

from orchestrator.core.workflow.operation_service import (
    OPERATION_STATUS_COMPLETED,
    OPERATION_STATUS_PENDING,
    OPERATION_STATUS_RUNNING,
    OPERATION_STATUS_WAITING_FOR_INPUT,
)
from orchestrator.core.workflow.runtime import WorkflowAdvanceOutcome
from orchestrator.core.workflow.type_catalog import (
    DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
    DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
    DEMO_PROOF_STEP_PREVIEW_CLEANUP,
    DEMO_PROOF_STEP_RECORDING,
    DEMO_PROOF_STEP_RELEASE,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.storage.models import WorkflowOperation

DEMO_PROOF_HANDLER_KEY = "demo_proof"
DEMO_PROOF_STEP_PREVIEW_LEASE = "preview_lease"


_DEMO_PROOF_EVENTS: dict[str, tuple[str, str | None, str]] = {
    "ProofLeaseAcquired": (
        DEMO_PROOF_STEP_PREVIEW_LEASE,
        DEMO_PROOF_STEP_RELEASE,
        "release_requested",
    ),
    "ServiceVerificationPassed": (
        DEMO_PROOF_STEP_RELEASE,
        DEMO_PROOF_STEP_RECORDING,
        "recording_requested",
    ),
    "RecordingCompleted": (
        DEMO_PROOF_STEP_RECORDING,
        DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        "evidence_upload_requested",
    ),
    "EvidenceUploaded": (
        DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        "pr_evidence_update_requested",
    ),
    "PREvidenceAttached": (
        DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        "preview_cleanup_requested",
    ),
    "PreviewCleanupCompleted": (
        DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        None,
        "demo_proof_completed",
    ),
    "RecordingFailureEvidenceCaptured": (
        DEMO_PROOF_STEP_RECORDING,
        DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        "failure_evidence_upload_requested",
    ),
    "FailureEvidenceUploaded": (
        DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        "pr_failure_evidence_update_requested",
    ),
    "PRFailureEvidenceAttached": (
        DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        "failure_preview_cleanup_requested",
    ),
    "FailurePreviewCleanupCompleted": (
        DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        None,
        "demo_proof_blocked_with_failure_evidence",
    ),
}


def _required_payload_string(payload: dict[str, object], field_name: str) -> str:
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise RuntimeError(f"Demo proof workflow requires {field_name}")
    return value


def _workflow_operation_status(*, session, workflow_id: str, operation_type: str) -> str | None:  # noqa: ANN001
    operation = session.execute(
        select(WorkflowOperation).where(
            WorkflowOperation.workflow_id == workflow_id,
            WorkflowOperation.operation_type == operation_type,
        )
    ).scalar_one_or_none()
    return str(getattr(operation, "status", "") or "").strip() or None


def _wait_for_operation_once(
    *,
    session,  # noqa: ANN001
    lifecycle,  # noqa: ANN001
    operation_type: str,
    run_id: str | None,
    proof_scope_id: str,
    summary: str,
) -> None:
    current_status = _workflow_operation_status(
        session=session,
        workflow_id=lifecycle.workflow.workflow_id,
        operation_type=operation_type,
    )
    if current_status in {
        OPERATION_STATUS_RUNNING,
        OPERATION_STATUS_WAITING_FOR_INPUT,
        OPERATION_STATUS_COMPLETED,
    }:
        return
    if current_status not in {None, OPERATION_STATUS_PENDING}:
        raise RuntimeError(
            f"Cannot start demo proof operation {operation_type} from current status {current_status}."
        )
    operation, attempt = lifecycle.start_operation_attempt(
        operation_type=operation_type,
        run_id=run_id,
        target_system="master_builder",
        target_ref=proof_scope_id,
        summary=summary,
    )
    lifecycle.wait_started_operation(
        operation=operation,
        attempt=attempt,
        summary=summary,
    )


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
        trigger_event = str(getattr(request.trigger, "event", "") or "").strip()
        if trigger_event:
            return self._advance_event(
                session=session,
                lifecycle=lifecycle,
                event=trigger_event,
                proof_scope_id=proof_scope_id,
                run_id=run_id,
            )
        _wait_for_operation_once(
            session=session,
            lifecycle=lifecycle,
            operation_type=DEMO_PROOF_STEP_PREVIEW_LEASE,
            run_id=run_id,
            proof_scope_id=proof_scope_id,
            summary=f"Acquire preview lease for demo proof scope {proof_scope_id}.",
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

    def _advance_event(
        self,
        *,
        session,  # noqa: ANN001
        lifecycle,  # noqa: ANN001
        event: str,
        proof_scope_id: str,
        run_id: str | None,
    ) -> WorkflowAdvanceOutcome:
        event_spec = _DEMO_PROOF_EVENTS.get(event)
        if event_spec is None:
            raise RuntimeError(f"Unsupported demo proof workflow event: {event}")
        completed_operation_type, next_operation_type, reason = event_spec
        lifecycle.complete_waiting_operation_attempt(
            operation_type=completed_operation_type,
            summary=f"Applied {event} for demo proof scope {proof_scope_id}.",
        )
        if next_operation_type is not None:
            _wait_for_operation_once(
                session=session,
                lifecycle=lifecycle,
                operation_type=next_operation_type,
                run_id=run_id,
                proof_scope_id=proof_scope_id,
                summary=f"Waiting for {next_operation_type} event for demo proof scope {proof_scope_id}.",
            )
        else:
            lifecycle.mark_completed_if_ready()
        return WorkflowAdvanceOutcome(
            handled=True,
            reason=reason,
            extra={
                "workflow_id": lifecycle.workflow.workflow_id,
                "execution_id": lifecycle.workflow.execution_id,
                "proof_scope_id": proof_scope_id,
            },
        )
