from __future__ import annotations

import pytest

from orchestrator.core.workflow.transitions import (
    TransitionRejectedError,
    attempt_creation_policy,
    transition_attempt,
    transition_input_request,
    transition_workflow,
)


def test_workflow_waiting_for_input_resumes_to_queued() -> None:
    assert (
        transition_workflow("waiting_for_input", "resume_attempt_created") == "queued"
    )


def test_workflow_blocked_can_resume_to_queued() -> None:
    assert transition_workflow("blocked", "resume_attempt_created") == "queued"


def test_attempt_running_can_wait_for_input() -> None:
    assert transition_attempt("running", "human_input_requested") == "waiting_for_input"


def test_attempt_waiting_for_input_can_resume_to_queued() -> None:
    assert transition_attempt("waiting_for_input", "resume_attempt_created") == "queued"


def test_input_request_answered_can_be_consumed() -> None:
    assert transition_input_request("answered", "resume_attempt_created") == "consumed"


def test_input_request_expired_cannot_be_consumed() -> None:
    with pytest.raises(TransitionRejectedError):
        transition_input_request("expired", "resume_attempt_created")


def test_attempt_creation_policy_allows_blocked_fresh_retry() -> None:
    policy = attempt_creation_policy(workflow_status="blocked", mode="fresh")
    assert policy.allowed is True
    assert policy.reuse_workflow is False


def test_attempt_creation_policy_reuses_waiting_workflow_only_for_resume() -> None:
    resume_policy = attempt_creation_policy(
        workflow_status="waiting_for_input", mode="resume"
    )
    assert resume_policy.allowed is True
    assert resume_policy.reuse_workflow is True

    restart_policy = attempt_creation_policy(
        workflow_status="waiting_for_input", mode="restart"
    )
    assert restart_policy.allowed is False
    assert restart_policy.reason == "waiting_for_input_requires_resume"
