from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.workflow.transitions import WorkflowTransitionError

DEMO_PROOF_STATE_REQUESTED = "requested"
DEMO_PROOF_STATE_LEASE_ACQUIRING = "lease_acquiring"
DEMO_PROOF_STATE_LEASE_ACQUIRED = "lease_acquired"
DEMO_PROOF_STATE_RELEASE_REQUESTED = "release_requested"
DEMO_PROOF_STATE_RELEASE_PROVISIONING = "release_provisioning"
DEMO_PROOF_STATE_RELEASE_LIVE = "release_live"
DEMO_PROOF_STATE_SERVICES_VERIFYING = "services_verifying"
DEMO_PROOF_STATE_SERVICES_VERIFIED = "services_verified"
DEMO_PROOF_STATE_RECORDING = "recording"
DEMO_PROOF_STATE_EVIDENCE_UPLOADING = "evidence_uploading"
DEMO_PROOF_STATE_EVIDENCE_UPLOADED = "evidence_uploaded"
DEMO_PROOF_STATE_PR_ATTACHING = "pr_attaching"
DEMO_PROOF_STATE_PR_ATTACHED = "pr_attached"
DEMO_PROOF_STATE_CLEANUP_SCHEDULED = "cleanup_scheduled"
DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING = "failure_evidence_uploading"
DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADED = "failure_evidence_uploaded"
DEMO_PROOF_STATE_FAILURE_PR_ATTACHING = "failure_pr_attaching"
DEMO_PROOF_STATE_FAILURE_PR_ATTACHED = "failure_pr_attached"
DEMO_PROOF_STATE_FAILURE_CLEANUP_SCHEDULED = "failure_cleanup_scheduled"
DEMO_PROOF_STATE_COMPLETE = "complete"
DEMO_PROOF_STATE_BLOCKED = "blocked"

DEMO_PROOF_TERMINAL_STATES = frozenset(
    {
        DEMO_PROOF_STATE_COMPLETE,
        DEMO_PROOF_STATE_BLOCKED,
    }
)

PREVIEW_LEASE_STATE_REQUESTED = "requested"
PREVIEW_LEASE_STATE_ACTIVE = "active"
PREVIEW_LEASE_STATE_PROVISIONING = "provisioning"
PREVIEW_LEASE_STATE_LIVE = "live"
PREVIEW_LEASE_STATE_RECORDING = "recording"
PREVIEW_LEASE_STATE_EVIDENCE_COMPLETE = "evidence_complete"
PREVIEW_LEASE_STATE_RELEASED = "released"
PREVIEW_LEASE_STATE_DESTROYING = "destroying"
PREVIEW_LEASE_STATE_DESTROYED = "destroyed"
PREVIEW_LEASE_STATE_FAILED = "failed"
PREVIEW_LEASE_STATE_SUPERSEDED = "superseded"
PREVIEW_LEASE_STATE_EXPIRED = "expired"
PREVIEW_LEASE_STATE_CLEANUP_FAILED = "cleanup_failed"

PREVIEW_LEASE_TERMINAL_STATES = frozenset(
    {
        PREVIEW_LEASE_STATE_DESTROYED,
        PREVIEW_LEASE_STATE_SUPERSEDED,
    }
)


@dataclass(frozen=True)
class _EventTransition:
    allowed_from: frozenset[str]
    next_state: str


_ALL_DEMO_PROOF_NON_TERMINAL_STATES = frozenset(
    {
        DEMO_PROOF_STATE_REQUESTED,
        DEMO_PROOF_STATE_LEASE_ACQUIRING,
        DEMO_PROOF_STATE_LEASE_ACQUIRED,
        DEMO_PROOF_STATE_RELEASE_REQUESTED,
        DEMO_PROOF_STATE_RELEASE_PROVISIONING,
        DEMO_PROOF_STATE_RELEASE_LIVE,
        DEMO_PROOF_STATE_SERVICES_VERIFYING,
        DEMO_PROOF_STATE_SERVICES_VERIFIED,
        DEMO_PROOF_STATE_RECORDING,
        DEMO_PROOF_STATE_EVIDENCE_UPLOADING,
        DEMO_PROOF_STATE_EVIDENCE_UPLOADED,
        DEMO_PROOF_STATE_PR_ATTACHING,
        DEMO_PROOF_STATE_PR_ATTACHED,
        DEMO_PROOF_STATE_CLEANUP_SCHEDULED,
        DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING,
        DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADED,
        DEMO_PROOF_STATE_FAILURE_PR_ATTACHING,
        DEMO_PROOF_STATE_FAILURE_PR_ATTACHED,
        DEMO_PROOF_STATE_FAILURE_CLEANUP_SCHEDULED,
    }
)

_DEMO_PROOF_TRANSITIONS: dict[str, _EventTransition] = {
    "DemoProofRequested": _EventTransition(
        frozenset({DEMO_PROOF_STATE_REQUESTED}),
        DEMO_PROOF_STATE_LEASE_ACQUIRING,
    ),
    "ProofLeaseAcquired": _EventTransition(
        frozenset({DEMO_PROOF_STATE_LEASE_ACQUIRING}),
        DEMO_PROOF_STATE_LEASE_ACQUIRED,
    ),
    "ProofLeaseSuperseded": _EventTransition(
        frozenset(
            {
                DEMO_PROOF_STATE_LEASE_ACQUIRING,
                DEMO_PROOF_STATE_LEASE_ACQUIRED,
                DEMO_PROOF_STATE_RELEASE_REQUESTED,
                DEMO_PROOF_STATE_RELEASE_PROVISIONING,
                DEMO_PROOF_STATE_RELEASE_LIVE,
                DEMO_PROOF_STATE_SERVICES_VERIFYING,
                DEMO_PROOF_STATE_SERVICES_VERIFIED,
                DEMO_PROOF_STATE_RECORDING,
            }
        ),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "ReleaseRequested": _EventTransition(
        frozenset({DEMO_PROOF_STATE_LEASE_ACQUIRED}),
        DEMO_PROOF_STATE_RELEASE_REQUESTED,
    ),
    "ReleaseProvisioning": _EventTransition(
        frozenset({DEMO_PROOF_STATE_RELEASE_REQUESTED}),
        DEMO_PROOF_STATE_RELEASE_PROVISIONING,
    ),
    "ReleaseLive": _EventTransition(
        frozenset({DEMO_PROOF_STATE_RELEASE_PROVISIONING}),
        DEMO_PROOF_STATE_RELEASE_LIVE,
    ),
    "ReleaseFailed": _EventTransition(
        frozenset(
            {
                DEMO_PROOF_STATE_RELEASE_REQUESTED,
                DEMO_PROOF_STATE_RELEASE_PROVISIONING,
                DEMO_PROOF_STATE_RELEASE_LIVE,
            }
        ),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "RouteReady": _EventTransition(
        frozenset({DEMO_PROOF_STATE_RELEASE_LIVE}),
        DEMO_PROOF_STATE_SERVICES_VERIFYING,
    ),
    "ServiceVerificationPassed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_SERVICES_VERIFYING}),
        DEMO_PROOF_STATE_SERVICES_VERIFIED,
    ),
    "ServiceVerificationFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_SERVICES_VERIFYING}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "RecordingStarted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_SERVICES_VERIFIED}),
        DEMO_PROOF_STATE_RECORDING,
    ),
    "RecordingCompleted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_RECORDING}),
        DEMO_PROOF_STATE_EVIDENCE_UPLOADING,
    ),
    "RecordingFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_RECORDING}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "RecordingFailureEvidenceCaptured": _EventTransition(
        frozenset({DEMO_PROOF_STATE_RECORDING}),
        DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING,
    ),
    "EvidenceUploadStarted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_EVIDENCE_UPLOADING}),
        DEMO_PROOF_STATE_EVIDENCE_UPLOADING,
    ),
    "EvidenceUploaded": _EventTransition(
        frozenset({DEMO_PROOF_STATE_EVIDENCE_UPLOADING}),
        DEMO_PROOF_STATE_EVIDENCE_UPLOADED,
    ),
    "EvidenceUploadFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_EVIDENCE_UPLOADING}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "PREvidenceAttachStarted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_EVIDENCE_UPLOADED}),
        DEMO_PROOF_STATE_PR_ATTACHING,
    ),
    "PREvidenceAttached": _EventTransition(
        frozenset({DEMO_PROOF_STATE_PR_ATTACHING}),
        DEMO_PROOF_STATE_PR_ATTACHED,
    ),
    "PREvidenceAttachFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_PR_ATTACHING}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "PreviewCleanupRequested": _EventTransition(
        frozenset({DEMO_PROOF_STATE_PR_ATTACHED}),
        DEMO_PROOF_STATE_CLEANUP_SCHEDULED,
    ),
    "PreviewCleanupCompleted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_CLEANUP_SCHEDULED}),
        DEMO_PROOF_STATE_COMPLETE,
    ),
    "PreviewCleanupFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_CLEANUP_SCHEDULED}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "FailureEvidenceUploadStarted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING}),
        DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING,
    ),
    "FailureEvidenceUploaded": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING}),
        DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADED,
    ),
    "FailureEvidenceUploadFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADING}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "PRFailureEvidenceAttachStarted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_EVIDENCE_UPLOADED}),
        DEMO_PROOF_STATE_FAILURE_PR_ATTACHING,
    ),
    "PRFailureEvidenceAttached": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_PR_ATTACHING}),
        DEMO_PROOF_STATE_FAILURE_PR_ATTACHED,
    ),
    "PRFailureEvidenceAttachFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_PR_ATTACHING}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "FailurePreviewCleanupRequested": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_PR_ATTACHED}),
        DEMO_PROOF_STATE_FAILURE_CLEANUP_SCHEDULED,
    ),
    "FailurePreviewCleanupCompleted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_CLEANUP_SCHEDULED}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "FailurePreviewCleanupFailed": _EventTransition(
        frozenset({DEMO_PROOF_STATE_FAILURE_CLEANUP_SCHEDULED}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
    "DemoProofCompleted": _EventTransition(
        frozenset({DEMO_PROOF_STATE_CLEANUP_SCHEDULED, DEMO_PROOF_STATE_COMPLETE}),
        DEMO_PROOF_STATE_COMPLETE,
    ),
    "DemoProofBlocked": _EventTransition(
        _ALL_DEMO_PROOF_NON_TERMINAL_STATES | frozenset({DEMO_PROOF_STATE_BLOCKED}),
        DEMO_PROOF_STATE_BLOCKED,
    ),
}

_PREVIEW_LEASE_TRANSITIONS: dict[str, _EventTransition] = {
    "PreviewLeaseActivated": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_REQUESTED}),
        PREVIEW_LEASE_STATE_ACTIVE,
    ),
    "PreviewLeaseProvisioningStarted": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_ACTIVE}),
        PREVIEW_LEASE_STATE_PROVISIONING,
    ),
    "PreviewLeaseLive": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_PROVISIONING}),
        PREVIEW_LEASE_STATE_LIVE,
    ),
    "PreviewLeaseRecordingStarted": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_LIVE}),
        PREVIEW_LEASE_STATE_RECORDING,
    ),
    "PreviewLeaseEvidenceCompleted": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_RECORDING}),
        PREVIEW_LEASE_STATE_EVIDENCE_COMPLETE,
    ),
    "PreviewLeaseReleased": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_EVIDENCE_COMPLETE}),
        PREVIEW_LEASE_STATE_RELEASED,
    ),
    "PreviewLeaseDestroying": _EventTransition(
        frozenset(
            {
                PREVIEW_LEASE_STATE_RELEASED,
                PREVIEW_LEASE_STATE_FAILED,
                PREVIEW_LEASE_STATE_SUPERSEDED,
                PREVIEW_LEASE_STATE_EXPIRED,
                PREVIEW_LEASE_STATE_CLEANUP_FAILED,
            }
        ),
        PREVIEW_LEASE_STATE_DESTROYING,
    ),
    "PreviewLeaseDestroyed": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_DESTROYING}),
        PREVIEW_LEASE_STATE_DESTROYED,
    ),
    "PreviewLeaseFailed": _EventTransition(
        frozenset(
            {
                PREVIEW_LEASE_STATE_ACTIVE,
                PREVIEW_LEASE_STATE_PROVISIONING,
                PREVIEW_LEASE_STATE_LIVE,
                PREVIEW_LEASE_STATE_RECORDING,
            }
        ),
        PREVIEW_LEASE_STATE_FAILED,
    ),
    "PreviewLeaseSuperseded": _EventTransition(
        frozenset(
            {
                PREVIEW_LEASE_STATE_ACTIVE,
                PREVIEW_LEASE_STATE_PROVISIONING,
                PREVIEW_LEASE_STATE_LIVE,
                PREVIEW_LEASE_STATE_FAILED,
            }
        ),
        PREVIEW_LEASE_STATE_SUPERSEDED,
    ),
    "PreviewLeaseExpired": _EventTransition(
        frozenset(
            {
                PREVIEW_LEASE_STATE_ACTIVE,
                PREVIEW_LEASE_STATE_PROVISIONING,
                PREVIEW_LEASE_STATE_LIVE,
                PREVIEW_LEASE_STATE_FAILED,
            }
        ),
        PREVIEW_LEASE_STATE_EXPIRED,
    ),
    "PreviewLeaseCleanupFailed": _EventTransition(
        frozenset({PREVIEW_LEASE_STATE_DESTROYING}),
        PREVIEW_LEASE_STATE_CLEANUP_FAILED,
    ),
}


def _transition_state(
    *,
    current_state: str,
    event: str,
    transitions: dict[str, _EventTransition],
    terminal_states: frozenset[str],
    entity: str,
) -> str:
    normalized_state = str(current_state or "").strip()
    normalized_event = str(event or "").strip()
    rule = transitions.get(normalized_event)
    if rule is None:
        raise WorkflowTransitionError(f"Unknown {entity} transition event: {event}")
    if normalized_state == rule.next_state:
        return normalized_state
    if normalized_state in terminal_states:
        raise WorkflowTransitionError(
            f"Cannot apply {normalized_event} to terminal {entity} state {normalized_state}"
        )
    if normalized_state not in rule.allowed_from:
        raise WorkflowTransitionError(
            f"Cannot apply {normalized_event} to {entity} in state {normalized_state}"
        )
    return rule.next_state


def transition_demo_proof_state(*, current_state: str, event: str) -> str:
    return _transition_state(
        current_state=current_state,
        event=event,
        transitions=_DEMO_PROOF_TRANSITIONS,
        terminal_states=DEMO_PROOF_TERMINAL_STATES,
        entity="demo proof",
    )


def transition_demo_proof(current_status: str, event: str) -> str:
    return transition_demo_proof_state(current_state=current_status, event=event)


def is_demo_proof_terminal(status: str) -> bool:
    return str(status or "").strip() in DEMO_PROOF_TERMINAL_STATES


def transition_preview_lease_state(*, current_state: str, event: str) -> str:
    return _transition_state(
        current_state=current_state,
        event=event,
        transitions=_PREVIEW_LEASE_TRANSITIONS,
        terminal_states=PREVIEW_LEASE_TERMINAL_STATES,
        entity="preview lease",
    )


def transition_preview_lease(current_status: str, event: str) -> str:
    return transition_preview_lease_state(current_state=current_status, event=event)


def is_preview_lease_terminal(status: str) -> bool:
    return str(status or "").strip() in PREVIEW_LEASE_TERMINAL_STATES
