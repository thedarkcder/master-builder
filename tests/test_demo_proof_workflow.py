from __future__ import annotations

import pytest

from orchestrator.core.qa.demo_proof_workflow import (
    DEMO_PROOF_STATE_BLOCKED,
    DEMO_PROOF_STATE_COMPLETE,
    DEMO_PROOF_STATE_EVIDENCE_UPLOADING,
    DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING,
    DEMO_PROOF_STATE_LEASE_ACQUIRING,
    DEMO_PROOF_STATE_REQUESTED,
    DEMO_PROOF_STATE_RECORDING,
    DEMO_PROOF_STATE_RECORDING_DEFERRED,
    PREVIEW_LEASE_STATE_DESTROYED,
    PREVIEW_LEASE_STATE_LIVE,
    PREVIEW_LEASE_STATE_RECORDING,
    PREVIEW_LEASE_STATE_REQUESTED,
    is_demo_proof_terminal,
    is_preview_lease_terminal,
    transition_demo_proof_state,
    transition_preview_lease_state,
)
from orchestrator.core.workflow.transitions import WorkflowTransitionError


def test_demo_proof_happy_path_reaches_complete_through_required_events() -> None:
    state = DEMO_PROOF_STATE_REQUESTED

    for event in (
        "DemoProofRequested",
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
        "RecordingCompleted",
        "EvidenceUploaded",
        "PREvidenceAttachStarted",
        "PREvidenceAttached",
        "PreviewCleanupRequested",
        "PreviewCleanupCompleted",
    ):
        state = transition_demo_proof_state(current_state=state, event=event)

    assert state == DEMO_PROOF_STATE_COMPLETE
    assert is_demo_proof_terminal(state)


def test_demo_proof_duplicate_event_that_already_advanced_state_is_idempotent() -> None:
    assert (
        transition_demo_proof_state(
            current_state=DEMO_PROOF_STATE_LEASE_ACQUIRING,
            event="DemoProofRequested",
        )
        == DEMO_PROOF_STATE_LEASE_ACQUIRING
    )


def test_demo_proof_rejects_out_of_order_upload_before_recording_completes() -> None:
    with pytest.raises(WorkflowTransitionError, match="Cannot apply EvidenceUploaded"):
        transition_demo_proof_state(
            current_state=DEMO_PROOF_STATE_REQUESTED,
            event="EvidenceUploaded",
        )


def test_demo_proof_failure_event_blocks_at_owned_boundary() -> None:
    state = transition_demo_proof_state(
        current_state=DEMO_PROOF_STATE_EVIDENCE_UPLOADING,
        event="EvidenceUploadFailed",
    )

    assert state == DEMO_PROOF_STATE_BLOCKED
    assert is_demo_proof_terminal(state)


def test_demo_proof_recording_failure_with_evidence_reports_failure_then_blocks() -> None:
    state = transition_demo_proof_state(
        current_state=DEMO_PROOF_STATE_REQUESTED,
        event="DemoProofRequested",
    )
    for event in (
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
    ):
        state = transition_demo_proof_state(current_state=state, event=event)

    state = transition_demo_proof_state(
        current_state=state,
        event="RecordingFailureEvidenceCaptured",
    )
    assert state == DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING

    for event in (
        "FailureEvidenceUploaded",
        "PRFailureEvidenceAttachStarted",
        "PRFailureEvidenceAttached",
        "FailurePreviewCleanupRequested",
        "FailurePreviewCleanupCompleted",
    ):
        state = transition_demo_proof_state(current_state=state, event=event)

    assert state == DEMO_PROOF_STATE_BLOCKED
    assert is_demo_proof_terminal(state)


def test_demo_proof_recording_failure_without_evidence_blocks_immediately() -> None:
    state = transition_demo_proof_state(
        current_state=DEMO_PROOF_STATE_RECORDING,
        event="RecordingFailed",
    )

    assert state == DEMO_PROOF_STATE_BLOCKED


def test_demo_proof_recording_deferred_can_resume_on_next_worker() -> None:
    state = transition_demo_proof_state(
        current_state=DEMO_PROOF_STATE_REQUESTED,
        event="DemoProofRequested",
    )
    for event in (
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
    ):
        state = transition_demo_proof_state(current_state=state, event=event)

    state = transition_demo_proof_state(current_state=state, event="RecordingDeferred")
    assert state == DEMO_PROOF_STATE_RECORDING_DEFERRED
    assert not is_demo_proof_terminal(state)

    state = transition_demo_proof_state(current_state=state, event="RecordingStarted")
    assert state == DEMO_PROOF_STATE_RECORDING


def test_demo_proof_terminal_state_rejects_late_non_duplicate_event() -> None:
    with pytest.raises(WorkflowTransitionError, match="terminal demo proof state blocked"):
        transition_demo_proof_state(
            current_state=DEMO_PROOF_STATE_BLOCKED,
            event="PreviewCleanupRequested",
        )


def test_preview_lease_happy_path_reaches_destroyed() -> None:
    state = PREVIEW_LEASE_STATE_REQUESTED

    for event in (
        "PreviewLeaseActivated",
        "PreviewLeaseProvisioningStarted",
        "PreviewLeaseLive",
        "PreviewLeaseRecordingStarted",
        "PreviewLeaseEvidenceCompleted",
        "PreviewLeaseReleased",
        "PreviewLeaseDestroying",
        "PreviewLeaseDestroyed",
    ):
        state = transition_preview_lease_state(current_state=state, event=event)

    assert state == PREVIEW_LEASE_STATE_DESTROYED
    assert is_preview_lease_terminal(state)


def test_preview_lease_rejects_recording_before_live() -> None:
    with pytest.raises(WorkflowTransitionError, match="Cannot apply PreviewLeaseRecordingStarted"):
        transition_preview_lease_state(
            current_state=PREVIEW_LEASE_STATE_REQUESTED,
            event="PreviewLeaseRecordingStarted",
        )


def test_preview_lease_cleanup_failure_can_be_retried() -> None:
    state = transition_preview_lease_state(
        current_state=PREVIEW_LEASE_STATE_LIVE,
        event="PreviewLeaseRecordingStarted",
    )
    assert state == PREVIEW_LEASE_STATE_RECORDING
    state = transition_preview_lease_state(
        current_state=state,
        event="PreviewLeaseFailed",
    )
    state = transition_preview_lease_state(
        current_state=state,
        event="PreviewLeaseDestroying",
    )
    state = transition_preview_lease_state(
        current_state=state,
        event="PreviewLeaseCleanupFailed",
    )

    assert (
        transition_preview_lease_state(
            current_state=state,
            event="PreviewLeaseDestroying",
        )
        == "destroying"
    )
