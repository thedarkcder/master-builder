from __future__ import annotations

import json

from sqlalchemy import select

from orchestrator.core.qa.demo_proof_workflow import DEMO_PROOF_STATE_REQUESTED, transition_demo_proof_state
from orchestrator.core.workflow.execution_projection import workflow_execution_id
from orchestrator.core.workflow.operation_service import (
    OPERATION_STATUS_FAILED,
    OPERATION_STATUS_COMPLETED,
    OPERATION_STATUS_PENDING,
    OPERATION_STATUS_RUNNING,
    OPERATION_STATUS_WAITING_FOR_INPUT,
    fail_workflow_operation,
)
from orchestrator.core.workflow.execution_status import mark_workflow_failed
from orchestrator.core.workflow.runtime import WorkflowAdvanceOutcome
from orchestrator.core.workflow.type_catalog import (
    DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
    DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
    DEMO_PROOF_STEP_PREVIEW_CLEANUP,
    DEMO_PROOF_STEP_RECORDING,
    DEMO_PROOF_STEP_RELEASE,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt

DEMO_PROOF_HANDLER_KEY = "demo_proof"
DEMO_PROOF_STEP_PREVIEW_LEASE = "preview_lease"
_DEMO_PROOF_STATE_DESCRIPTION_KEY = "demo_proof_state"
_DEMO_PROOF_EVENT_HISTORY_DESCRIPTION_KEY = "demo_proof_events"
_DEMO_PROOF_EVENT_METADATA_DESCRIPTION_KEY = "demo_proof_event_metadata"
_RECORDING_WORKFLOW_DESCRIPTION_KEY = "recording_workflows"


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

_DEMO_PROOF_OBSERVATION_EVENTS: dict[str, tuple[str, str]] = {
    "ReleaseRequested": (DEMO_PROOF_STEP_RELEASE, "release_requested_observed"),
    "ReleaseProvisioning": (DEMO_PROOF_STEP_RELEASE, "release_provisioning_observed"),
    "ReleaseLive": (DEMO_PROOF_STEP_RELEASE, "release_live_observed"),
    "RouteReady": (DEMO_PROOF_STEP_RELEASE, "route_ready_observed"),
    "RecordingStarted": (DEMO_PROOF_STEP_RECORDING, "recording_started_observed"),
    "EvidenceUploadStarted": (DEMO_PROOF_STEP_EVIDENCE_UPLOAD, "evidence_upload_started_observed"),
    "PREvidenceAttachStarted": (DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE, "pr_evidence_attach_started_observed"),
    "PreviewCleanupRequested": (DEMO_PROOF_STEP_PREVIEW_CLEANUP, "preview_cleanup_requested_observed"),
    "FailureEvidenceUploadStarted": (
        DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        "failure_evidence_upload_started_observed",
    ),
    "PRFailureEvidenceAttachStarted": (
        DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        "pr_failure_evidence_attach_started_observed",
    ),
    "FailurePreviewCleanupRequested": (
        DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        "failure_preview_cleanup_requested_observed",
    ),
}

_DEMO_PROOF_FAILURE_EVENTS: dict[str, tuple[str, str, str]] = {
    "ReleaseFailed": (DEMO_PROOF_STEP_RELEASE, "release_failed", "release_failed"),
    "ServiceVerificationFailed": (
        DEMO_PROOF_STEP_RELEASE,
        "service_verification_failed",
        "service_verification_failed",
    ),
    "RecordingFailed": (DEMO_PROOF_STEP_RECORDING, "recording_failed", "recording_failed"),
    "EvidenceUploadFailed": (
        DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        "evidence_upload_failed",
        "evidence_upload_failed",
    ),
    "PREvidenceAttachFailed": (
        DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        "pr_evidence_attach_failed",
        "pr_evidence_attach_failed",
    ),
    "PreviewCleanupFailed": (
        DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        "preview_cleanup_failed",
        "preview_cleanup_failed",
    ),
    "FailureEvidenceUploadFailed": (
        DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        "failure_evidence_upload_failed",
        "failure_evidence_upload_failed",
    ),
    "PRFailureEvidenceAttachFailed": (
        DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        "pr_failure_evidence_attach_failed",
        "pr_failure_evidence_attach_failed",
    ),
    "FailurePreviewCleanupFailed": (
        DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        "failure_preview_cleanup_failed",
        "failure_preview_cleanup_failed",
    ),
}

_DEMO_PROOF_BLOCKING_COMPLETION_EVENTS = frozenset({"FailurePreviewCleanupCompleted"})
_PR_EVIDENCE_REQUEST_EVENTS = frozenset({"EvidenceUploaded", "FailureEvidenceUploaded"})
_SUCCESS_TERMINAL_METADATA_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "ReleaseLive": ("release_id",),
    "EvidenceUploaded": ("artifact_urls",),
    "PREvidenceAttached": ("pr_url",),
    "PreviewCleanupCompleted": ("release_id",),
}
_FAILURE_TERMINAL_METADATA_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "ReleaseLive": ("release_id",),
    "FailureEvidenceUploaded": ("artifact_urls", "capture_targets"),
    "PRFailureEvidenceAttached": ("pr_url",),
    "FailurePreviewCleanupCompleted": ("release_id",),
}
_RECORDING_EVENT_STATES = {
    "ServiceVerificationPassed": "waiting_for_recording",
    "RecordingStarted": "recording",
    "RecordingCompleted": "recorded",
    "RecordingFailed": "failed",
    "RecordingFailureEvidenceCaptured": "failed",
}


def _required_payload_string(payload: dict[str, object], field_name: str) -> str:
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise RuntimeError(f"Demo proof workflow requires {field_name}")
    return value


def _workflow_id(*, workflow_type, request) -> str:  # noqa: ANN001
    return workflow_execution_id(
        workflow_type_key=workflow_type.workflow_type_key,
        execution_key=request.execution.key,
    )


def _decode_demo_proof_description(raw_value: object) -> dict[str, object]:
    if raw_value is None:
        return {}
    if isinstance(raw_value, dict):
        return dict(raw_value)
    normalized = str(raw_value or "").strip()
    if not normalized:
        return {}
    try:
        decoded = json.loads(normalized)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Demo proof workflow description is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise RuntimeError("Demo proof workflow description must be a JSON object")
    return dict(decoded)


def _normalized_capture_targets(required_capture_targets: list[object]) -> list[str]:
    targets: list[str] = []
    seen: set[str] = set()
    for item in required_capture_targets:
        target = str(item or "").strip()
        if not target or target in seen:
            continue
        seen.add(target)
        targets.append(target)
    return targets or ["browser", "ios", "android"]


def _event_metadata(payload: dict[str, object]) -> dict[str, object] | None:
    raw_metadata = payload.get("event_metadata")
    if raw_metadata is None:
        return None
    if not isinstance(raw_metadata, dict):
        raise RuntimeError("Demo proof event_metadata must be a JSON object")
    try:
        json.dumps(raw_metadata, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Demo proof event_metadata must be JSON serializable") from exc
    return dict(raw_metadata)


def _recording_workflow_descriptions(
    *,
    previous_description: dict[str, object],
    required_capture_targets: list[object],
    event: str | None,
) -> list[dict[str, str]]:
    existing_state_by_target: dict[str, str] = {}
    for item in list(previous_description.get(_RECORDING_WORKFLOW_DESCRIPTION_KEY) or []):
        if not isinstance(item, dict):
            continue
        capture_target = str(item.get("capture_target") or "").strip()
        state = str(item.get("state") or "").strip()
        if capture_target and state:
            existing_state_by_target[capture_target] = state
    next_state = _RECORDING_EVENT_STATES.get(str(event or "").strip())
    workflows: list[dict[str, str]] = []
    for capture_target in _normalized_capture_targets(required_capture_targets):
        workflows.append(
            {
                "capture_target": capture_target,
                "state": next_state or existing_state_by_target.get(capture_target, "planned"),
            }
        )
    return workflows


def _current_demo_proof_state(*, session, workflow_id: str) -> str:  # noqa: ANN001
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        return DEMO_PROOF_STATE_REQUESTED
    payload = _decode_demo_proof_description(workflow.source_description)
    state = str(payload.get(_DEMO_PROOF_STATE_DESCRIPTION_KEY) or "").strip()
    if not state:
        raise RuntimeError("Existing demo proof workflow is missing durable demo_proof_state")
    return state


def _demo_proof_description(
    *,
    previous_description: dict[str, object],
    proof_scope_id: str,
    commit_sha: str,
    run_id: str | None,
    pr_url: str | None,
    required_capture_targets: list[object],
    request_id: str,
    state: str,
    event: str | None,
    event_metadata: dict[str, object] | None,
) -> dict[str, object]:
    payload = dict(previous_description)
    events = list(payload.get(_DEMO_PROOF_EVENT_HISTORY_DESCRIPTION_KEY) or [])
    metadata_entries = list(payload.get(_DEMO_PROOF_EVENT_METADATA_DESCRIPTION_KEY) or [])
    previous_last_event = str(events[-1]) if events else ""
    if event and (not events or str(events[-1]) != event):
        events.append(event)
    if event and event_metadata and previous_last_event != event:
        metadata_entries.append({"event": event, "metadata": event_metadata})
    payload.update(
        {
            "proof_scope_id": proof_scope_id,
            "commit_sha": commit_sha,
            "run_id": run_id,
            "pr_url": pr_url,
            "required_capture_targets": [str(target) for target in required_capture_targets],
            _RECORDING_WORKFLOW_DESCRIPTION_KEY: _recording_workflow_descriptions(
                previous_description=previous_description,
                required_capture_targets=required_capture_targets,
                event=event,
            ),
            "request_id": request_id,
            _DEMO_PROOF_STATE_DESCRIPTION_KEY: state,
            _DEMO_PROOF_EVENT_HISTORY_DESCRIPTION_KEY: events[-100:],
            _DEMO_PROOF_EVENT_METADATA_DESCRIPTION_KEY: metadata_entries[-100:],
        }
    )
    return payload


def _persist_demo_proof_state(
    *,
    session,  # noqa: ANN001
    workflow_id: str,
    description: dict[str, object],
) -> None:
    workflow = session.get(WorkflowExecution, workflow_id)
    if workflow is None:
        raise RuntimeError("Demo proof workflow did not create a durable workflow execution")
    workflow.source_description = json.dumps(description, sort_keys=True)
    session.flush()


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


def _wait_summary(
    *,
    operation_type: str,
    proof_scope_id: str,
    required_capture_targets: list[object],
) -> str:
    if operation_type == DEMO_PROOF_STEP_RECORDING:
        targets = ", ".join(_normalized_capture_targets(required_capture_targets))
        return (
            f"Waiting for recording event for demo proof scope {proof_scope_id} "
            f"across required capture target(s): {targets}."
        )
    return f"Waiting for {operation_type} event for demo proof scope {proof_scope_id}."


def _require_pr_url_for_pr_evidence_event(*, event: str, pr_url: str | None, proof_scope_id: str) -> None:
    if event not in _PR_EVIDENCE_REQUEST_EVENTS:
        return
    if str(pr_url or "").strip():
        return
    raise RuntimeError(f"Demo proof scope {proof_scope_id} requires PR URL before PR evidence update.")


def _metadata_by_event(description: dict[str, object]) -> dict[str, dict[str, object]]:
    metadata: dict[str, dict[str, object]] = {}
    for item in list(description.get(_DEMO_PROOF_EVENT_METADATA_DESCRIPTION_KEY) or []):
        if not isinstance(item, dict):
            continue
        event = str(item.get("event") or "").strip()
        raw_metadata = item.get("metadata")
        if event and isinstance(raw_metadata, dict):
            metadata[event] = dict(raw_metadata)
    return metadata


def _metadata_field_is_present(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_metadata_field_is_present(item) for item in value)
    return value is not None


def _metadata_string_set(value: object) -> set[str]:
    if isinstance(value, list):
        return {str(item or "").strip() for item in value if str(item or "").strip()}
    normalized = str(value or "").strip()
    return {normalized} if normalized else set()


def _require_terminal_proof_metadata(
    *,
    event: str,
    description: dict[str, object],
    proof_scope_id: str,
) -> None:
    requirements = (
        _SUCCESS_TERMINAL_METADATA_REQUIREMENTS
        if event == "PreviewCleanupCompleted"
        else _FAILURE_TERMINAL_METADATA_REQUIREMENTS
        if event == "FailurePreviewCleanupCompleted"
        else None
    )
    if requirements is None:
        return
    metadata = _metadata_by_event(description)
    missing: list[str] = []
    for required_event, required_fields in requirements.items():
        event_metadata = metadata.get(required_event) or {}
        for field in required_fields:
            if not _metadata_field_is_present(event_metadata.get(field)):
                missing.append(f"{required_event}.{field}")
    if missing:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires auditable metadata before terminal completion: "
            + ", ".join(missing)
        )
    if event == "PreviewCleanupCompleted":
        required_targets = set(_normalized_capture_targets(list(description.get("required_capture_targets") or [])))
        evidence_targets = _metadata_string_set(metadata.get("EvidenceUploaded", {}).get("capture_targets"))
        missing_targets = sorted(required_targets - evidence_targets)
        if missing_targets:
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} evidence metadata is missing required capture target(s): "
                + ", ".join(missing_targets)
            )


def _observe_waiting_operation_event(
    *,
    session,  # noqa: ANN001
    lifecycle,  # noqa: ANN001
    operation_type: str,
    event: str,
    proof_scope_id: str,
) -> None:
    current_status = _workflow_operation_status(
        session=session,
        workflow_id=lifecycle.workflow.workflow_id,
        operation_type=operation_type,
    )
    if current_status in {OPERATION_STATUS_RUNNING, OPERATION_STATUS_WAITING_FOR_INPUT}:
        return
    raise RuntimeError(
        f"Cannot apply demo proof event {event} to operation {operation_type} from current status {current_status} "
        f"for proof scope {proof_scope_id}."
    )


def _fail_waiting_operation_event(
    *,
    session,  # noqa: ANN001
    lifecycle,  # noqa: ANN001
    operation_type: str,
    event: str,
    proof_scope_id: str,
    category: str,
) -> str:
    current_status = _workflow_operation_status(
        session=session,
        workflow_id=lifecycle.workflow.workflow_id,
        operation_type=operation_type,
    )
    message = f"Demo proof event {event} blocked proof scope {proof_scope_id}."
    if current_status == OPERATION_STATUS_FAILED:
        return message
    if current_status != OPERATION_STATUS_WAITING_FOR_INPUT:
        raise RuntimeError(
            f"Cannot apply demo proof failure event {event} to operation {operation_type} "
            f"from current status {current_status} for proof scope {proof_scope_id}."
        )
    operation = session.execute(
        select(WorkflowOperation)
        .where(
            WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id,
            WorkflowOperation.operation_type == operation_type,
            WorkflowOperation.status == OPERATION_STATUS_WAITING_FOR_INPUT,
        )
        .order_by(WorkflowOperation.updated_at.desc())
        .limit(1)
    ).scalar_one()
    attempt = session.execute(
        select(WorkflowOperationAttempt)
        .where(
            WorkflowOperationAttempt.operation_id == operation.operation_id,
            WorkflowOperationAttempt.status == OPERATION_STATUS_WAITING_FOR_INPUT,
        )
        .order_by(WorkflowOperationAttempt.attempt_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if attempt is None:
        raise RuntimeError(
            f"Cannot apply demo proof failure event {event} to operation {operation_type}; "
            f"no waiting attempt exists for proof scope {proof_scope_id}."
        )
    fail_workflow_operation(
        session,
        operation=operation,
        attempt=attempt,
        category=category,
        message=message,
    )
    mark_workflow_failed(workflow=lifecycle.workflow, message=message)
    session.flush()
    return message


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
        workflow_id = _workflow_id(workflow_type=workflow_type, request=request)
        current_state = _current_demo_proof_state(session=session, workflow_id=workflow_id)
        existing_workflow = session.get(WorkflowExecution, workflow_id)
        previous_description = (
            _decode_demo_proof_description(existing_workflow.source_description)
            if existing_workflow is not None
            else {}
        )
        trigger_event = str(getattr(request.trigger, "event", "") or "").strip()
        state_event = trigger_event or "DemoProofRequested"
        event_metadata = _event_metadata(request.payload)
        try:
            next_state = transition_demo_proof_state(current_state=current_state, event=state_event)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(str(exc)) from exc
        next_description = _demo_proof_description(
            previous_description=previous_description,
            proof_scope_id=proof_scope_id,
            commit_sha=commit_sha,
            run_id=run_id,
            pr_url=pr_url,
            required_capture_targets=required_capture_targets,
            request_id=request_id,
            state=next_state,
            event=state_event,
            event_metadata=event_metadata,
        )
        lifecycle.ensure_execution(
            display_name=f"Demo proof {proof_scope_id}",
            description=next_description,
        )
        if trigger_event:
            result = self._advance_event(
                session=session,
                lifecycle=lifecycle,
                event=trigger_event,
                proof_scope_id=proof_scope_id,
                run_id=run_id,
                pr_url=pr_url,
                required_capture_targets=required_capture_targets,
                description=next_description,
            )
            _persist_demo_proof_state(
                session=session,
                workflow_id=workflow_id,
                description=next_description,
            )
            session.commit()
            return result
        _wait_for_operation_once(
            session=session,
            lifecycle=lifecycle,
            operation_type=DEMO_PROOF_STEP_PREVIEW_LEASE,
            run_id=run_id,
            proof_scope_id=proof_scope_id,
            summary=f"Acquire preview lease for demo proof scope {proof_scope_id}.",
        )
        _persist_demo_proof_state(
            session=session,
            workflow_id=workflow_id,
            description=next_description,
        )
        session.commit()
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
        pr_url: str | None,
        required_capture_targets: list[object],
        description: dict[str, object],
    ) -> WorkflowAdvanceOutcome:
        observation_spec = _DEMO_PROOF_OBSERVATION_EVENTS.get(event)
        if observation_spec is not None:
            operation_type, reason = observation_spec
            _observe_waiting_operation_event(
                session=session,
                lifecycle=lifecycle,
                operation_type=operation_type,
                event=event,
                proof_scope_id=proof_scope_id,
            )
            return WorkflowAdvanceOutcome(
                handled=True,
                reason=reason,
                extra={
                    "workflow_id": lifecycle.workflow.workflow_id,
                    "execution_id": lifecycle.workflow.execution_id,
                    "proof_scope_id": proof_scope_id,
                },
            )
        failure_spec = _DEMO_PROOF_FAILURE_EVENTS.get(event)
        if failure_spec is not None:
            operation_type, category, reason = failure_spec
            message = _fail_waiting_operation_event(
                session=session,
                lifecycle=lifecycle,
                operation_type=operation_type,
                event=event,
                proof_scope_id=proof_scope_id,
                category=category,
            )
            return WorkflowAdvanceOutcome(
                handled=True,
                reason=reason,
                failed=True,
                extra={
                    "workflow_id": lifecycle.workflow.workflow_id,
                    "execution_id": lifecycle.workflow.execution_id,
                    "proof_scope_id": proof_scope_id,
                    "error_message": message,
                },
            )
        event_spec = _DEMO_PROOF_EVENTS.get(event)
        if event_spec is None:
            raise RuntimeError(f"Unsupported demo proof workflow event: {event}")
        completed_operation_type, next_operation_type, reason = event_spec
        _require_pr_url_for_pr_evidence_event(event=event, pr_url=pr_url, proof_scope_id=proof_scope_id)
        _require_terminal_proof_metadata(event=event, description=description, proof_scope_id=proof_scope_id)
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
                summary=_wait_summary(
                    operation_type=next_operation_type,
                    proof_scope_id=proof_scope_id,
                    required_capture_targets=required_capture_targets,
                ),
            )
        elif event in _DEMO_PROOF_BLOCKING_COMPLETION_EVENTS:
            message = f"Demo proof recorded failure evidence for proof scope {proof_scope_id}."
            mark_workflow_failed(workflow=lifecycle.workflow, message=message)
            session.flush()
        else:
            lifecycle.mark_completed_if_ready()
        return WorkflowAdvanceOutcome(
            handled=True,
            reason=reason,
            failed=event in _DEMO_PROOF_BLOCKING_COMPLETION_EVENTS,
            extra={
                "workflow_id": lifecycle.workflow.workflow_id,
                "execution_id": lifecycle.workflow.execution_id,
                "proof_scope_id": proof_scope_id,
            },
        )
