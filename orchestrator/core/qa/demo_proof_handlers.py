from __future__ import annotations

from collections import defaultdict
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
_SUPPORTED_TRIGGER_MODES = frozenset(
    {
        "from_run",
        "from_pr",
        "from_release",
        "retry_recording",
        "cleanup_only",
    }
)


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
    "RecordingDeferred": (DEMO_PROOF_STEP_RECORDING, "recording_deferred_observed"),
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
    "ProofLeaseAcquired": ("release_id", "release_commit_sha", "demo_proof_lease"),
    "ReleaseLive": ("release_id", "release_commit_sha", "demo_proof_lease"),
    "ServiceVerificationPassed": (
        "release_id",
        "release_commit_sha",
        "required_service_kinds",
        "service_urls",
    ),
    "RecordingCompleted": ("artifact_urls", "recordings"),
    "EvidenceUploaded": ("artifact_urls", "recordings"),
    "PREvidenceAttached": ("pr_url", "pr_body_sha256"),
    "PreviewCleanupCompleted": ("release_id", "cleanup_status", "cleanup_mode", "cleanup_evidence"),
}
_FAILURE_TERMINAL_METADATA_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "ProofLeaseAcquired": ("release_id", "release_commit_sha", "demo_proof_lease"),
    "ReleaseLive": ("release_id", "release_commit_sha", "demo_proof_lease"),
    "ServiceVerificationPassed": (
        "release_id",
        "release_commit_sha",
        "required_service_kinds",
        "service_urls",
    ),
    "RecordingFailureEvidenceCaptured": ("artifact_urls", "capture_targets", "failure_evidence"),
    "FailureEvidenceUploaded": ("artifact_urls", "capture_targets", "failure_evidence"),
    "PRFailureEvidenceAttached": ("pr_url", "pr_body_sha256"),
    "FailurePreviewCleanupCompleted": ("release_id", "cleanup_status", "cleanup_mode", "cleanup_evidence"),
}
_RECORDING_EVENT_STATES = {
    "ServiceVerificationPassed": "waiting_for_recording",
    "RecordingStarted": "recording",
    "RecordingDeferred": "deferred",
    "RecordingCompleted": "recorded",
    "RecordingFailed": "failed",
    "RecordingFailureEvidenceCaptured": "failed",
}


def _required_payload_string(payload: dict[str, object], field_name: str) -> str:
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise RuntimeError(f"Demo proof workflow requires {field_name}")
    return value


def _required_trigger_mode(payload: dict[str, object]) -> str:
    trigger_mode = _required_payload_string(payload, "trigger_mode")
    if trigger_mode not in _SUPPORTED_TRIGGER_MODES:
        raise RuntimeError(
            "Demo proof workflow requires trigger_mode to be one of: "
            + ", ".join(sorted(_SUPPORTED_TRIGGER_MODES))
        )
    return trigger_mode


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


def _normalized_required_recording_counts(
    *,
    required_capture_targets: list[object],
    value: object,
) -> dict[str, int]:
    targets = _normalized_capture_targets(required_capture_targets)
    counts: dict[str, int] = {target: 1 for target in targets}
    if value is None:
        return counts
    if not isinstance(value, dict):
        raise RuntimeError("Demo proof required_recording_counts must be a JSON object")
    target_set = set(targets)
    for raw_target, raw_count in value.items():
        target = str(raw_target or "").strip()
        if target not in target_set:
            raise RuntimeError(f"Demo proof required_recording_counts contains unsupported target: {target}")
        if isinstance(raw_count, bool):
            raise RuntimeError(f"Demo proof required_recording_counts.{target} must be a positive integer")
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Demo proof required_recording_counts.{target} must be a positive integer") from exc
        if count < 1:
            raise RuntimeError(f"Demo proof required_recording_counts.{target} must be a positive integer")
        counts[target] = count
    return counts


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
    event_metadata: dict[str, object] | None,
) -> list[dict[str, str]]:
    existing_state_by_target: dict[str, str] = {}
    for item in list(previous_description.get(_RECORDING_WORKFLOW_DESCRIPTION_KEY) or []):
        if not isinstance(item, dict):
            continue
        capture_target = str(item.get("capture_target") or "").strip()
        state = str(item.get("state") or "").strip()
        if capture_target and state:
            existing_state_by_target[capture_target] = state
    normalized_event = str(event or "").strip()
    next_state = _RECORDING_EVENT_STATES.get(normalized_event)
    recorded_targets = set(_metadata_string_list((event_metadata or {}).get("recorded_capture_targets")))
    remaining_targets = set(_metadata_string_list((event_metadata or {}).get("remaining_capture_targets")))
    workflows: list[dict[str, str]] = []
    for capture_target in _normalized_capture_targets(required_capture_targets):
        if normalized_event == "RecordingDeferred":
            if capture_target in recorded_targets:
                state = "recorded"
            elif capture_target in remaining_targets:
                state = "deferred"
            else:
                state = existing_state_by_target.get(capture_target, next_state or "planned")
            workflows.append({"capture_target": capture_target, "state": state})
            continue
        workflows.append(
            {
                "capture_target": capture_target,
                "state": next_state or existing_state_by_target.get(capture_target, "planned"),
            }
        )
    return workflows


def _require_immutable_description_value(
    *,
    previous_description: dict[str, object],
    field: str,
    next_value: object,
    proof_scope_id: str,
) -> None:
    if field not in previous_description:
        return
    previous_value = previous_description.get(field)
    if previous_value == next_value:
        return
    raise RuntimeError(
        f"Demo proof workflow {field} cannot change for proof scope {proof_scope_id}: "
        f"{previous_value!r} != {next_value!r}"
    )


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
    trigger_mode: str,
    run_id: str | None,
    pr_url: str | None,
    required_capture_targets: list[object],
    required_recording_counts: dict[str, int],
    request_id: str,
    state: str,
    event: str | None,
    event_metadata: dict[str, object] | None,
) -> dict[str, object]:
    payload = dict(previous_description)
    events = list(payload.get(_DEMO_PROOF_EVENT_HISTORY_DESCRIPTION_KEY) or [])
    metadata_entries = list(payload.get(_DEMO_PROOF_EVENT_METADATA_DESCRIPTION_KEY) or [])
    previous_last_event = str(events[-1]) if events else ""
    previous_trigger_mode = str(payload.get("trigger_mode") or "").strip()
    if previous_trigger_mode and previous_trigger_mode != trigger_mode:
        raise RuntimeError(
            f"Demo proof workflow trigger_mode cannot change for proof scope {proof_scope_id}: "
            f"{previous_trigger_mode} != {trigger_mode}"
        )
    _require_immutable_description_value(
        previous_description=previous_description,
        field="commit_sha",
        next_value=commit_sha,
        proof_scope_id=proof_scope_id,
    )
    _require_immutable_description_value(
        previous_description=previous_description,
        field="run_id",
        next_value=run_id,
        proof_scope_id=proof_scope_id,
    )
    _require_immutable_description_value(
        previous_description=previous_description,
        field="pr_url",
        next_value=pr_url,
        proof_scope_id=proof_scope_id,
    )
    _require_immutable_description_value(
        previous_description=previous_description,
        field="required_capture_targets",
        next_value=[str(target) for target in required_capture_targets],
        proof_scope_id=proof_scope_id,
    )
    _require_immutable_description_value(
        previous_description=previous_description,
        field="required_recording_counts",
        next_value=dict(required_recording_counts),
        proof_scope_id=proof_scope_id,
    )
    if event and (not events or str(events[-1]) != event):
        events.append(event)
    if event and event_metadata and previous_last_event != event:
        metadata_entries.append({"event": event, "metadata": event_metadata})
    payload.update(
        {
            "proof_scope_id": proof_scope_id,
            "commit_sha": commit_sha,
            "trigger_mode": trigger_mode,
            "run_id": run_id,
            "pr_url": pr_url,
            "required_capture_targets": [str(target) for target in required_capture_targets],
            "required_recording_counts": dict(required_recording_counts),
            _RECORDING_WORKFLOW_DESCRIPTION_KEY: _recording_workflow_descriptions(
                previous_description=previous_description,
                required_capture_targets=required_capture_targets,
                event=event,
                event_metadata=event_metadata,
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


def _metadata_string_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item or "").strip() for item in value if str(item or "").strip()]
    normalized = str(value or "").strip()
    return [normalized] if normalized else []


def _metadata_string(value: object) -> str:
    return str(value or "").strip()


_RECORDING_ARTIFACT_REQUIRED_FIELDS = frozenset(
    {
        "artifact_url",
        "object_key",
        "capture_reference",
        "content_sha256",
        "release_commit_sha",
        "release_context_sha256",
    }
)
_FAILURE_EVIDENCE_REQUIRED_FIELDS = frozenset(
    {
        *_RECORDING_ARTIFACT_REQUIRED_FIELDS,
        "error_message",
    }
)
_DEMO_PROOF_LEASE_REQUIRED_FIELDS = frozenset(
    {
        "lease_id",
        "proof_scope_id",
        "commit_sha",
        "state",
        "acquired_at",
        "expires_at",
    }
)


def _recording_artifacts_by_target(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, list):
        return {}
    artifacts_by_target: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        capture_target = str(item.get("capture_target") or "").strip()
        artifact_metadata = {
            field: str(item.get(field) or "").strip()
            for field in _RECORDING_ARTIFACT_REQUIRED_FIELDS
        }
        if capture_target and all(artifact_metadata.values()):
            artifacts_by_target[capture_target] = artifact_metadata
    return artifacts_by_target


def _recording_artifact_counts_by_target(value: object) -> dict[str, int]:
    if not isinstance(value, list):
        return {}
    counts_by_target: dict[str, int] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        capture_target = str(item.get("capture_target") or "").strip()
        artifact_metadata = {
            field: str(item.get(field) or "").strip()
            for field in _RECORDING_ARTIFACT_REQUIRED_FIELDS
        }
        if capture_target and all(artifact_metadata.values()):
            counts_by_target[capture_target] = counts_by_target.get(capture_target, 0) + 1
    return counts_by_target


def _failure_evidence_by_target(value: object) -> dict[str, dict[str, str]]:
    if not isinstance(value, list):
        return {}
    evidence_by_target: dict[str, dict[str, str]] = {}
    for item in value:
        if not isinstance(item, dict):
            continue
        capture_target = str(item.get("capture_target") or "").strip()
        evidence_metadata = {
            field: str(item.get(field) or "").strip()
            for field in _FAILURE_EVIDENCE_REQUIRED_FIELDS
        }
        if capture_target and all(evidence_metadata.values()):
            evidence_by_target[capture_target] = evidence_metadata
    return evidence_by_target


def _artifact_lineage_by_target(*, value: object, required_fields: frozenset[str]) -> dict[str, list[tuple[str, ...]]]:
    if not isinstance(value, list):
        return {}
    fields = tuple(sorted(required_fields))
    lineage_by_target: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for item in value:
        if not isinstance(item, dict):
            continue
        capture_target = _metadata_string(item.get("capture_target"))
        field_values = tuple(_metadata_string(item.get(field)) for field in fields)
        if capture_target and all(field_values):
            lineage_by_target[capture_target].append(field_values)
    return {target: sorted(lineage) for target, lineage in lineage_by_target.items()}


def _require_matching_artifact_lineage(
    *,
    source_metadata: dict[str, object],
    downstream_metadata: dict[str, object],
    source_event: str,
    downstream_event: str,
    evidence_field: str,
    required_fields: frozenset[str],
    proof_scope_id: str,
) -> None:
    source_lineage = _artifact_lineage_by_target(
        value=source_metadata.get(evidence_field),
        required_fields=required_fields,
    )
    downstream_lineage = _artifact_lineage_by_target(
        value=downstream_metadata.get(evidence_field),
        required_fields=required_fields,
    )
    mismatched_targets = sorted(
        target
        for target in set(source_lineage) | set(downstream_lineage)
        if source_lineage.get(target) != downstream_lineage.get(target)
    )
    if mismatched_targets:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} {downstream_event}.{evidence_field} must match "
            f"{source_event}.{evidence_field} for capture target(s): " + ", ".join(mismatched_targets)
        )


def _verified_service_kinds(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    service_kinds: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        service_kind = str(item.get("service_kind") or "").strip()
        url = str(item.get("url") or "").strip()
        status = str(item.get("status") or "").strip()
        if service_kind and url and status == "active":
            service_kinds.add(service_kind)
    return service_kinds


def _demo_proof_lease_metadata(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    lease_metadata = {
        field: str(value.get(field) or "").strip()
        for field in _DEMO_PROOF_LEASE_REQUIRED_FIELDS
    }
    if all(lease_metadata.values()):
        return lease_metadata
    return {}


def _require_cleanup_evidence_metadata(*, value: object, event: str, proof_scope_id: str) -> None:
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires {event}.cleanup_evidence to be a JSON object"
        )
    required = (
        "release_id",
        "lease_id",
        "proof_scope_id",
        "commit_sha",
        "cleanup_status",
        "cleanup_mode",
        "lease_state",
    )
    missing = [field for field in required if not str(value.get(field) or "").strip()]
    if missing:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires cleanup evidence metadata before terminal completion: "
            + ", ".join(f"{event}.cleanup_evidence.{field}" for field in missing)
        )
    lease_state = str(value.get("lease_state") or "").strip()
    if lease_state not in {"destroyed", "expired", "ttl_scheduled"}:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} cleanup evidence has unsupported lease_state: {lease_state}"
        )


def _cleanup_resource_refs(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        resource_type = str(item.get("resource_type") or "").strip()
        resource_id = str(item.get("resource_id") or "").strip()
        cleanup_action = str(item.get("cleanup_action") or "").strip()
        if resource_type and resource_id and cleanup_action:
            refs.append(
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "cleanup_action": cleanup_action,
                }
            )
    return refs


def _require_cleanup_resource_refs(
    *,
    value: object,
    release_id: str,
    lease_state: str,
    event: str,
    proof_scope_id: str,
) -> None:
    refs = _cleanup_resource_refs(value)
    if not refs:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires {event}.cleanup_evidence.resource_refs before "
            "terminal cleanup completion"
        )
    invalid_actions = sorted(
        {
            ref["cleanup_action"]
            for ref in refs
            if ref["cleanup_action"] != lease_state
        }
    )
    if invalid_actions:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} cleanup resource refs must use lease_state {lease_state}: "
            + ", ".join(invalid_actions)
        )
    has_release_ref = any(
        ref["resource_type"] == "release" and ref["resource_id"] == release_id
        for ref in refs
    )
    if not has_release_ref:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires {event}.cleanup_evidence.resource_refs to include "
            f"release {release_id}"
        )
    has_preview_resource_ref = any(ref["resource_type"] != "release" for ref in refs)
    if not has_preview_resource_ref:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires {event}.cleanup_evidence.resource_refs to identify "
            "at least one preview resource outside the release record"
        )


def _require_lease_scope_identity(
    *,
    acquired_metadata: dict[str, object],
    release_metadata: dict[str, object],
    cleanup_metadata: dict[str, object],
    event: str,
    proof_scope_id: str,
) -> None:
    acquired_lease = _demo_proof_lease_metadata(acquired_metadata.get("demo_proof_lease"))
    if not acquired_lease:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires ProofLeaseAcquired.demo_proof_lease metadata before "
            "terminal completion: "
            + ", ".join(
                f"ProofLeaseAcquired.demo_proof_lease.{field}"
                for field in sorted(_DEMO_PROOF_LEASE_REQUIRED_FIELDS)
            )
        )
    release_lease = _demo_proof_lease_metadata(release_metadata.get("demo_proof_lease"))
    if not release_lease:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires ReleaseLive.demo_proof_lease metadata before terminal "
            "completion: "
            + ", ".join(f"ReleaseLive.demo_proof_lease.{field}" for field in sorted(_DEMO_PROOF_LEASE_REQUIRED_FIELDS))
        )
    release_id = _metadata_string(release_metadata.get("release_id"))
    acquired_release_id = _metadata_string(acquired_metadata.get("release_id"))
    if acquired_release_id != release_id:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} acquired lease must reference ReleaseLive.release_id {release_id}: "
            f"ProofLeaseAcquired.release_id={acquired_release_id or '<missing>'}"
        )
    release_proof_scope_id = _metadata_string(release_lease.get("proof_scope_id"))
    if release_proof_scope_id != proof_scope_id:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} live release lease must reference proof scope {proof_scope_id}: "
            f"ReleaseLive.demo_proof_lease.proof_scope_id={release_proof_scope_id or '<missing>'}"
        )
    acquired_proof_scope_id = _metadata_string(acquired_lease.get("proof_scope_id"))
    if acquired_proof_scope_id != proof_scope_id:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} acquired lease must reference proof scope {proof_scope_id}: "
            f"ProofLeaseAcquired.demo_proof_lease.proof_scope_id={acquired_proof_scope_id or '<missing>'}"
        )
    cleanup_evidence = cleanup_metadata.get("cleanup_evidence")
    cleanup_proof_scope_id = (
        _metadata_string(cleanup_evidence.get("proof_scope_id"))
        if isinstance(cleanup_evidence, dict)
        else ""
    )
    if cleanup_proof_scope_id != proof_scope_id:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} cleanup evidence must reference proof scope {proof_scope_id}: "
            f"{event}.cleanup_evidence.proof_scope_id={cleanup_proof_scope_id or '<missing>'}"
        )
    release_lease_id = _metadata_string(release_lease.get("lease_id"))
    acquired_lease_id = _metadata_string(acquired_lease.get("lease_id"))
    if acquired_lease_id != release_lease_id:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} acquired lease must match ReleaseLive.demo_proof_lease.lease_id "
            f"{release_lease_id}: ProofLeaseAcquired.demo_proof_lease.lease_id={acquired_lease_id or '<missing>'}, "
            f"ReleaseLive.demo_proof_lease.lease_id={release_lease_id or '<missing>'}"
        )
    cleanup_lease_id = (
        _metadata_string(cleanup_evidence.get("lease_id"))
        if isinstance(cleanup_evidence, dict)
        else ""
    )
    if cleanup_lease_id != release_lease_id:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} cleanup evidence must reference ReleaseLive.demo_proof_lease.lease_id "
            f"{release_lease_id}: {event}.cleanup_evidence.lease_id={cleanup_lease_id or '<missing>'}"
        )
    release_commit_sha = _metadata_string(release_metadata.get("release_commit_sha")).lower()
    acquired_commit_sha = _metadata_string(acquired_metadata.get("release_commit_sha")).lower()
    acquired_lease_commit_sha = _metadata_string(acquired_lease.get("commit_sha")).lower()
    lease_commit_sha = _metadata_string(release_lease.get("commit_sha")).lower()
    cleanup_commit_sha = (
        _metadata_string(cleanup_evidence.get("commit_sha")).lower()
        if isinstance(cleanup_evidence, dict)
        else ""
    )
    if (
        acquired_commit_sha != release_commit_sha
        or acquired_lease_commit_sha != release_commit_sha
        or lease_commit_sha != release_commit_sha
        or cleanup_commit_sha != release_commit_sha
    ):
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} lease metadata must reference ReleaseLive.release_commit_sha "
            f"{release_commit_sha}: "
            f"ProofLeaseAcquired.release_commit_sha={acquired_commit_sha or '<missing>'}, "
            f"ProofLeaseAcquired.demo_proof_lease.commit_sha={acquired_lease_commit_sha or '<missing>'}, "
            f"ReleaseLive.demo_proof_lease.commit_sha={lease_commit_sha or '<missing>'}, "
            f"{event}.cleanup_evidence.commit_sha={cleanup_commit_sha or '<missing>'}"
        )


def _require_cleanup_release_identity(
    *,
    release_metadata: dict[str, object],
    cleanup_metadata: dict[str, object],
    event: str,
    proof_scope_id: str,
) -> None:
    release_id = _metadata_string(release_metadata.get("release_id"))
    cleanup_release_id = _metadata_string(cleanup_metadata.get("release_id"))
    cleanup_evidence = cleanup_metadata.get("cleanup_evidence")
    cleanup_evidence_release_id = (
        _metadata_string(cleanup_evidence.get("release_id"))
        if isinstance(cleanup_evidence, dict)
        else ""
    )
    mismatched = [
        f"{event}.release_id={cleanup_release_id or '<missing>'}",
        f"{event}.cleanup_evidence.release_id={cleanup_evidence_release_id or '<missing>'}",
    ]
    if cleanup_release_id == release_id and cleanup_evidence_release_id == release_id:
        return
    raise RuntimeError(
        f"Demo proof scope {proof_scope_id} cleanup evidence must reference ReleaseLive.release_id {release_id}: "
        + ", ".join(mismatched)
    )


def _require_pr_evidence_matches_uploaded_artifacts(
    *,
    uploaded_metadata: dict[str, object],
    pr_metadata: dict[str, object],
    pr_event: str,
    proof_scope_id: str,
) -> None:
    uploaded_urls = set(_metadata_string_list(uploaded_metadata.get("artifact_urls")))
    attached_urls = set(_metadata_string_list(pr_metadata.get("artifact_urls")))
    missing_urls = sorted(uploaded_urls - attached_urls)
    extra_urls = sorted(attached_urls - uploaded_urls)
    if missing_urls:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} PR evidence metadata is missing uploaded artifact URL(s): "
            + ", ".join(missing_urls)
        )
    if extra_urls:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} {pr_event}.artifact_urls contains URL(s) that were not uploaded: "
            + ", ".join(extra_urls)
        )


def _require_pr_artifact_url_checks(
    *,
    uploaded_metadata: dict[str, object],
    pr_metadata: dict[str, object],
    pr_event: str,
    proof_scope_id: str,
) -> None:
    if _metadata_string(pr_metadata.get("artifact_url_check_status")) != "passed":
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} {pr_event} artifact URL checks must pass before terminal completion"
        )
    uploaded_urls = set(_metadata_string_list(uploaded_metadata.get("artifact_urls")))
    checked_urls = set(_metadata_string_list(pr_metadata.get("checked_artifact_urls")))
    missing_urls = sorted(uploaded_urls - checked_urls)
    extra_urls = sorted(checked_urls - uploaded_urls)
    if missing_urls:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} {pr_event}.checked_artifact_urls is missing checked uploaded "
            "artifact URL(s): " + ", ".join(missing_urls)
        )
    if extra_urls:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} {pr_event}.checked_artifact_urls contains URL(s) that were "
            "not uploaded: " + ", ".join(extra_urls)
        )


def _require_pr_evidence_targets_workflow_pr(
    *,
    description: dict[str, object],
    pr_metadata: dict[str, object],
    pr_event: str,
    proof_scope_id: str,
) -> None:
    workflow_pr_url = _metadata_string(description.get("pr_url"))
    evidence_pr_url = _metadata_string(pr_metadata.get("pr_url"))
    if evidence_pr_url == workflow_pr_url:
        return
    raise RuntimeError(
        f"Demo proof scope {proof_scope_id} {pr_event}.pr_url must match workflow pr_url "
        f"{workflow_pr_url or '<missing>'}: {pr_event}.pr_url={evidence_pr_url or '<missing>'}"
    )


def _require_pr_body_sha256_metadata(*, pr_metadata: dict[str, object], pr_event: str, proof_scope_id: str) -> None:
    value = _metadata_string(pr_metadata.get("pr_body_sha256")).lower()
    if len(value) == 64 and all(char in "0123456789abcdef" for char in value):
        return
    raise RuntimeError(
        f"Demo proof scope {proof_scope_id} {pr_event}.pr_body_sha256 must be a SHA-256 hex digest"
    )


def _require_recording_completion_metadata(*, description: dict[str, object], proof_scope_id: str) -> None:
    metadata = _metadata_by_event(description)
    recording_metadata = metadata.get("RecordingCompleted", {})
    required_targets = set(_normalized_capture_targets(list(description.get("required_capture_targets") or [])))
    recording_targets = _metadata_string_set(recording_metadata.get("capture_targets"))
    missing_targets = sorted(required_targets - recording_targets)
    if missing_targets:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} recording completion metadata is missing required capture target(s): "
            + ", ".join(missing_targets)
        )
    artifact_urls = _metadata_string_list(recording_metadata.get("artifact_urls"))
    if len(artifact_urls) < len(required_targets):
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} recording completion requires at least {len(required_targets)} "
            f"artifact URL(s) for required capture target(s), got {len(artifact_urls)}."
        )
    artifacts_by_target = _recording_artifacts_by_target(recording_metadata.get("recordings"))
    missing_target_artifacts = sorted(target for target in required_targets if target not in artifacts_by_target)
    if missing_target_artifacts:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} recording completion metadata is missing artifact metadata "
            "for required capture target(s): " + ", ".join(missing_target_artifacts)
        )
    recording_counts_by_target = _recording_artifact_counts_by_target(recording_metadata.get("recordings"))
    required_counts = _normalized_required_recording_counts(
        required_capture_targets=list(required_targets),
        value=description.get("required_recording_counts"),
    )
    missing_count_targets = sorted(
        (target, required_count, recording_counts_by_target.get(target, 0))
        for target, required_count in required_counts.items()
        if recording_counts_by_target.get(target, 0) < required_count
    )
    if missing_count_targets:
        target, required_count, actual_count = missing_count_targets[0]
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires {required_count} recording artifact(s) "
            f"for capture target {target}, got {actual_count}."
        )


def _require_failure_evidence_target_metadata(
    *,
    description: dict[str, object],
    failure_metadata: dict[str, object],
    proof_scope_id: str,
) -> dict[str, dict[str, str]]:
    required_targets = set(_normalized_capture_targets(list(description.get("required_capture_targets") or [])))
    failure_targets = _metadata_string_set(failure_metadata.get("capture_targets"))
    missing_targets = sorted(required_targets - failure_targets)
    if missing_targets:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} failure evidence metadata is missing required capture target(s): "
            + ", ".join(missing_targets)
        )
    artifact_urls = _metadata_string_list(failure_metadata.get("artifact_urls"))
    if len(artifact_urls) < len(required_targets):
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires at least {len(required_targets)} failure artifact URL(s) "
            f"for required capture target(s), got {len(artifact_urls)}."
        )
    if len(set(artifact_urls)) < len(required_targets):
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires at least {len(required_targets)} distinct failure "
            "artifact URL(s) for required capture target(s)."
        )
    evidence_by_target = _failure_evidence_by_target(failure_metadata.get("failure_evidence"))
    missing_diagnostic_targets = sorted(target for target in required_targets if target not in evidence_by_target)
    if missing_diagnostic_targets:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} failure evidence metadata is missing uploaded diagnostic "
            "artifact metadata "
            "for required capture target(s): " + ", ".join(missing_diagnostic_targets)
        )
    artifact_urls_by_target = {
        target: evidence_by_target[target]["artifact_url"]
        for target in required_targets
    }
    if len(set(artifact_urls_by_target.values())) < len(required_targets):
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires distinct failure artifact URL mappings for "
            "required capture target(s)."
        )
    return evidence_by_target


def _require_service_verification_metadata(
    *,
    release_metadata: dict[str, object],
    service_metadata: dict[str, object],
    proof_scope_id: str,
) -> None:
    release_id = _metadata_string(release_metadata.get("release_id"))
    service_release_id = _metadata_string(service_metadata.get("release_id"))
    if service_release_id != release_id:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} service verification must reference ReleaseLive.release_id "
            f"{release_id}: ServiceVerificationPassed.release_id={service_release_id or '<missing>'}"
        )
    release_commit_sha = _metadata_string(release_metadata.get("release_commit_sha")).lower()
    service_commit_sha = _metadata_string(service_metadata.get("release_commit_sha")).lower()
    if service_commit_sha != release_commit_sha:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} service verification must reference ReleaseLive.release_commit_sha "
            f"{release_commit_sha}: ServiceVerificationPassed.release_commit_sha={service_commit_sha or '<missing>'}"
        )
    required_kinds = set(_metadata_string_list(service_metadata.get("required_service_kinds")))
    if not required_kinds:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} requires ServiceVerificationPassed.required_service_kinds "
            "before terminal completion"
        )
    verified_kinds = _verified_service_kinds(service_metadata.get("service_urls"))
    missing_kinds = sorted(required_kinds - verified_kinds)
    if missing_kinds:
        raise RuntimeError(
            f"Demo proof scope {proof_scope_id} ServiceVerificationPassed.service_urls is missing active "
            "verified service kind(s): " + ", ".join(missing_kinds)
        )


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
    release_metadata = metadata.get("ReleaseLive", {})
    _require_service_verification_metadata(
        release_metadata=release_metadata,
        service_metadata=metadata.get("ServiceVerificationPassed", {}),
        proof_scope_id=proof_scope_id,
    )
    if event in {"PreviewCleanupCompleted", "FailurePreviewCleanupCompleted"}:
        cleanup_metadata = metadata.get(event, {})
        _require_cleanup_evidence_metadata(
            value=cleanup_metadata.get("cleanup_evidence"),
            event=event,
            proof_scope_id=proof_scope_id,
        )
        _require_cleanup_release_identity(
            release_metadata=release_metadata,
            cleanup_metadata=cleanup_metadata,
            event=event,
            proof_scope_id=proof_scope_id,
        )
        _require_lease_scope_identity(
            acquired_metadata=metadata.get("ProofLeaseAcquired", {}),
            release_metadata=release_metadata,
            cleanup_metadata=cleanup_metadata,
            event=event,
            proof_scope_id=proof_scope_id,
        )
        cleanup_evidence = cleanup_metadata.get("cleanup_evidence")
        _require_cleanup_resource_refs(
            value=cleanup_evidence.get("resource_refs") if isinstance(cleanup_evidence, dict) else None,
            release_id=_metadata_string(release_metadata.get("release_id")),
            lease_state=_metadata_string(cleanup_evidence.get("lease_state")) if isinstance(cleanup_evidence, dict) else "",
            event=event,
            proof_scope_id=proof_scope_id,
        )
    if event == "PreviewCleanupCompleted":
        required_targets = set(_normalized_capture_targets(list(description.get("required_capture_targets") or [])))
        recording_metadata = metadata.get("RecordingCompleted", {})
        evidence_metadata = metadata.get("EvidenceUploaded", {})
        evidence_targets = _metadata_string_set(evidence_metadata.get("capture_targets"))
        missing_targets = sorted(required_targets - evidence_targets)
        if missing_targets:
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} evidence metadata is missing required capture target(s): "
                + ", ".join(missing_targets)
            )
        artifact_urls = _metadata_string_list(evidence_metadata.get("artifact_urls"))
        if len(artifact_urls) < len(required_targets):
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} requires at least {len(required_targets)} playable artifact URL(s) "
                f"for required capture target(s), got {len(artifact_urls)}."
            )
        if len(set(artifact_urls)) < len(required_targets):
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} requires at least {len(required_targets)} "
                "distinct playable artifact URL(s) for required capture target(s)."
            )
        artifacts_by_target = _recording_artifacts_by_target(evidence_metadata.get("recordings"))
        missing_target_artifacts = sorted(target for target in required_targets if target not in artifacts_by_target)
        if missing_target_artifacts:
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} evidence metadata is missing uploaded artifact metadata "
                "for required capture target(s): " + ", ".join(missing_target_artifacts)
            )
        artifact_urls_by_target = {
            target: artifacts_by_target[target]["artifact_url"]
            for target in required_targets
        }
        if len(set(artifact_urls_by_target.values())) < len(required_targets):
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} requires distinct playable artifact URL mappings for "
                "required capture target(s)."
            )
        _require_matching_artifact_lineage(
            source_metadata=recording_metadata,
            downstream_metadata=evidence_metadata,
            source_event="RecordingCompleted",
            downstream_event="EvidenceUploaded",
            evidence_field="recordings",
            required_fields=_RECORDING_ARTIFACT_REQUIRED_FIELDS,
            proof_scope_id=proof_scope_id,
        )
        _require_pr_evidence_matches_uploaded_artifacts(
            uploaded_metadata=evidence_metadata,
            pr_metadata=metadata.get("PREvidenceAttached", {}),
            pr_event="PREvidenceAttached",
            proof_scope_id=proof_scope_id,
        )
        _require_pr_artifact_url_checks(
            uploaded_metadata=evidence_metadata,
            pr_metadata=metadata.get("PREvidenceAttached", {}),
            pr_event="PREvidenceAttached",
            proof_scope_id=proof_scope_id,
        )
        _require_pr_evidence_targets_workflow_pr(
            description=description,
            pr_metadata=metadata.get("PREvidenceAttached", {}),
            pr_event="PREvidenceAttached",
            proof_scope_id=proof_scope_id,
        )
        _require_pr_body_sha256_metadata(
            pr_metadata=metadata.get("PREvidenceAttached", {}),
            pr_event="PREvidenceAttached",
            proof_scope_id=proof_scope_id,
        )
        recording_counts_by_target = _recording_artifact_counts_by_target(evidence_metadata.get("recordings"))
        required_counts = _normalized_required_recording_counts(
            required_capture_targets=list(required_targets),
            value=description.get("required_recording_counts"),
        )
        missing_count_targets = sorted(
            (target, required_count, recording_counts_by_target.get(target, 0))
            for target, required_count in required_counts.items()
            if recording_counts_by_target.get(target, 0) < required_count
        )
        if missing_count_targets:
            target, required_count, actual_count = missing_count_targets[0]
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} requires {required_count} recording artifact(s) "
                f"for capture target {target}, got {actual_count}."
            )
        release_commit_sha = _metadata_string(release_metadata.get("release_commit_sha"))
        mismatched_commit_targets = sorted(
            target
            for target in required_targets
            if artifacts_by_target[target]["release_commit_sha"].lower() != release_commit_sha.lower()
        )
        if mismatched_commit_targets:
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} recording metadata release commit does not match "
                "ReleaseLive.release_commit_sha for capture target(s): "
                + ", ".join(mismatched_commit_targets)
            )
    if event == "FailurePreviewCleanupCompleted":
        captured_failure_metadata = metadata.get("RecordingFailureEvidenceCaptured", {})
        failure_metadata = metadata.get("FailureEvidenceUploaded", {})
        evidence_by_target = _require_failure_evidence_target_metadata(
            description=description,
            failure_metadata=failure_metadata,
            proof_scope_id=proof_scope_id,
        )
        _require_matching_artifact_lineage(
            source_metadata=captured_failure_metadata,
            downstream_metadata=failure_metadata,
            source_event="RecordingFailureEvidenceCaptured",
            downstream_event="FailureEvidenceUploaded",
            evidence_field="failure_evidence",
            required_fields=_FAILURE_EVIDENCE_REQUIRED_FIELDS,
            proof_scope_id=proof_scope_id,
        )
        release_commit_sha = _metadata_string(release_metadata.get("release_commit_sha"))
        mismatched_commit_targets = sorted(
            target
            for target in evidence_by_target
            if evidence_by_target[target]["release_commit_sha"].lower() != release_commit_sha.lower()
        )
        if mismatched_commit_targets:
            raise RuntimeError(
                f"Demo proof scope {proof_scope_id} failure evidence metadata release commit does not match "
                "ReleaseLive.release_commit_sha for capture target(s): "
                + ", ".join(mismatched_commit_targets)
            )
        _require_pr_evidence_matches_uploaded_artifacts(
            uploaded_metadata=failure_metadata,
            pr_metadata=metadata.get("PRFailureEvidenceAttached", {}),
            pr_event="PRFailureEvidenceAttached",
            proof_scope_id=proof_scope_id,
        )
        _require_pr_artifact_url_checks(
            uploaded_metadata=failure_metadata,
            pr_metadata=metadata.get("PRFailureEvidenceAttached", {}),
            pr_event="PRFailureEvidenceAttached",
            proof_scope_id=proof_scope_id,
        )
        _require_pr_evidence_targets_workflow_pr(
            description=description,
            pr_metadata=metadata.get("PRFailureEvidenceAttached", {}),
            pr_event="PRFailureEvidenceAttached",
            proof_scope_id=proof_scope_id,
        )
        _require_pr_body_sha256_metadata(
            pr_metadata=metadata.get("PRFailureEvidenceAttached", {}),
            pr_event="PRFailureEvidenceAttached",
            proof_scope_id=proof_scope_id,
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
        trigger_mode = _required_trigger_mode(request.payload)
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
        required_recording_counts = _normalized_required_recording_counts(
            required_capture_targets=required_capture_targets,
            value=request.payload.get("required_recording_counts"),
        )
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
            trigger_mode=trigger_mode,
            run_id=run_id,
            pr_url=pr_url,
            required_capture_targets=required_capture_targets,
            required_recording_counts=required_recording_counts,
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
        if event == "RecordingCompleted":
            _require_recording_completion_metadata(description=description, proof_scope_id=proof_scope_id)
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
