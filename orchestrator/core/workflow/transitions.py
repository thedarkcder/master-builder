from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


WORKFLOW_STATUS_QUEUED = "queued"
WORKFLOW_STATUS_RUNNING = "running"
WORKFLOW_STATUS_WAITING_FOR_INPUT = "waiting_for_input"
WORKFLOW_STATUS_BLOCKED = "blocked"
WORKFLOW_STATUS_SUCCEEDED = "succeeded"
WORKFLOW_STATUS_FAILED = "failed"
WORKFLOW_STATUS_CANCELLED = "cancelled"

ATTEMPT_STATUS_QUEUED = "queued"
ATTEMPT_STATUS_RUNNING = "running"
ATTEMPT_STATUS_WAITING_FOR_INPUT = "waiting_for_input"
ATTEMPT_STATUS_BLOCKED = "blocked"
ATTEMPT_STATUS_SUCCEEDED = "succeeded"
ATTEMPT_STATUS_FAILED = "failed"
ATTEMPT_STATUS_CANCELLED = "cancelled"

INPUT_STATUS_PENDING = "pending"
INPUT_STATUS_ANSWERED = "answered"
INPUT_STATUS_CONSUMED = "consumed"
INPUT_STATUS_EXPIRED = "expired"
INPUT_STATUS_CANCELLED = "cancelled"

ACTIVE_WORKFLOW_STATUSES = {
    WORKFLOW_STATUS_QUEUED,
    WORKFLOW_STATUS_RUNNING,
    WORKFLOW_STATUS_WAITING_FOR_INPUT,
    WORKFLOW_STATUS_BLOCKED,
}
TERMINAL_WORKFLOW_STATUSES = {
    WORKFLOW_STATUS_SUCCEEDED,
    WORKFLOW_STATUS_FAILED,
    WORKFLOW_STATUS_CANCELLED,
}
ACTIVE_ATTEMPT_STATUSES = {
    ATTEMPT_STATUS_QUEUED,
    ATTEMPT_STATUS_RUNNING,
}
TERMINAL_ATTEMPT_STATUSES = {
    ATTEMPT_STATUS_SUCCEEDED,
    ATTEMPT_STATUS_FAILED,
    ATTEMPT_STATUS_CANCELLED,
}
TERMINAL_INPUT_STATUSES = {
    INPUT_STATUS_CONSUMED,
    INPUT_STATUS_EXPIRED,
    INPUT_STATUS_CANCELLED,
}

ATTEMPT_ENTRY_MODES = frozenset({"fresh", "restart", "resume"})
AttemptEntryMode = Literal["fresh", "restart", "resume"]


@dataclass(frozen=True)
class AttemptCreationPolicy:
    allowed: bool
    reuse_workflow: bool
    reason: str | None = None


class WorkflowTransitionError(ValueError):
    pass


TransitionRejectedError = WorkflowTransitionError


@dataclass(frozen=True)
class TransitionRule:
    allowed_from: frozenset[str]
    next_state: str


_WORKFLOW_TRANSITIONS: dict[str, TransitionRule] = {
    "workflow_enqueued": TransitionRule(frozenset({WORKFLOW_STATUS_QUEUED}), WORKFLOW_STATUS_QUEUED),
    "attempt_started": TransitionRule(
        frozenset({WORKFLOW_STATUS_QUEUED, WORKFLOW_STATUS_WAITING_FOR_INPUT}),
        WORKFLOW_STATUS_RUNNING,
    ),
    "human_input_requested": TransitionRule(
        frozenset({WORKFLOW_STATUS_RUNNING}),
        WORKFLOW_STATUS_WAITING_FOR_INPUT,
    ),
    "resume_attempt_created": TransitionRule(
        frozenset({WORKFLOW_STATUS_WAITING_FOR_INPUT, WORKFLOW_STATUS_BLOCKED}),
        WORKFLOW_STATUS_QUEUED,
    ),
    "input_expired": TransitionRule(
        frozenset({WORKFLOW_STATUS_WAITING_FOR_INPUT}),
        WORKFLOW_STATUS_BLOCKED,
    ),
    "workflow_succeeded": TransitionRule(
        frozenset({WORKFLOW_STATUS_RUNNING}),
        WORKFLOW_STATUS_SUCCEEDED,
    ),
    "workflow_failed": TransitionRule(
        frozenset({WORKFLOW_STATUS_RUNNING}),
        WORKFLOW_STATUS_FAILED,
    ),
    "workflow_cancelled": TransitionRule(
        frozenset(ACTIVE_WORKFLOW_STATUSES),
        WORKFLOW_STATUS_CANCELLED,
    ),
}

_ATTEMPT_TRANSITIONS: dict[str, TransitionRule] = {
    "attempt_created": TransitionRule(frozenset({ATTEMPT_STATUS_QUEUED}), ATTEMPT_STATUS_QUEUED),
    "attempt_started": TransitionRule(
        frozenset({ATTEMPT_STATUS_QUEUED}),
        ATTEMPT_STATUS_RUNNING,
    ),
    "human_input_requested": TransitionRule(
        frozenset({ATTEMPT_STATUS_RUNNING}),
        ATTEMPT_STATUS_WAITING_FOR_INPUT,
    ),
    "resume_attempt_created": TransitionRule(
        frozenset({ATTEMPT_STATUS_WAITING_FOR_INPUT, ATTEMPT_STATUS_BLOCKED}),
        ATTEMPT_STATUS_QUEUED,
    ),
    "input_expired": TransitionRule(
        frozenset({ATTEMPT_STATUS_WAITING_FOR_INPUT}),
        ATTEMPT_STATUS_BLOCKED,
    ),
    "attempt_succeeded": TransitionRule(
        frozenset({ATTEMPT_STATUS_RUNNING}),
        ATTEMPT_STATUS_SUCCEEDED,
    ),
    "attempt_failed": TransitionRule(
        frozenset({ATTEMPT_STATUS_RUNNING}),
        ATTEMPT_STATUS_FAILED,
    ),
    "attempt_cancelled": TransitionRule(
        frozenset(ACTIVE_ATTEMPT_STATUSES),
        ATTEMPT_STATUS_CANCELLED,
    ),
}

_INPUT_TRANSITIONS: dict[str, TransitionRule] = {
    "human_input_requested": TransitionRule(
        frozenset({INPUT_STATUS_PENDING}),
        INPUT_STATUS_PENDING,
    ),
    "human_input_answered": TransitionRule(
        frozenset({INPUT_STATUS_PENDING}),
        INPUT_STATUS_ANSWERED,
    ),
    "resume_attempt_created": TransitionRule(
        frozenset({INPUT_STATUS_ANSWERED}),
        INPUT_STATUS_CONSUMED,
    ),
    "input_expired": TransitionRule(
        frozenset({INPUT_STATUS_PENDING}),
        INPUT_STATUS_EXPIRED,
    ),
    "input_cancelled": TransitionRule(
        frozenset({INPUT_STATUS_PENDING, INPUT_STATUS_ANSWERED}),
        INPUT_STATUS_CANCELLED,
    ),
}


def _next_state(*, current_state: str, event: str, rules: dict[str, TransitionRule], entity: str) -> str:
    rule = rules.get(event)
    if rule is None:
        raise WorkflowTransitionError(f"Unknown {entity} transition event: {event}")
    if current_state not in rule.allowed_from:
        raise WorkflowTransitionError(
            f"Cannot apply {event} to {entity} in state {current_state}"
        )
    return rule.next_state


def transition_workflow_state(*, current_state: str, event: str) -> str:
    return _next_state(current_state=current_state, event=event, rules=_WORKFLOW_TRANSITIONS, entity="workflow")


def transition_attempt_state(*, current_state: str, event: str) -> str:
    return _next_state(current_state=current_state, event=event, rules=_ATTEMPT_TRANSITIONS, entity="attempt")


def transition_input_request_state(*, current_state: str, event: str) -> str:
    return _next_state(current_state=current_state, event=event, rules=_INPUT_TRANSITIONS, entity="input request")


def transition_workflow(current_status: str, event: str) -> str:
    return transition_workflow_state(current_state=current_status, event=event)


def transition_attempt(current_status: str, event: str) -> str:
    return transition_attempt_state(current_state=current_status, event=event)


def transition_input_request(current_status: str, event: str) -> str:
    return transition_input_request_state(current_state=current_status, event=event)


def is_workflow_terminal(status: str) -> bool:
    return status in TERMINAL_WORKFLOW_STATUSES


def is_attempt_terminal(status: str) -> bool:
    return status in TERMINAL_ATTEMPT_STATUSES


def is_attempt_worker_active(status: str) -> bool:
    return status in ACTIVE_ATTEMPT_STATUSES


def attempt_creation_policy(*, workflow_status: str, mode: str) -> AttemptCreationPolicy:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ATTEMPT_ENTRY_MODES:
        return AttemptCreationPolicy(allowed=False, reuse_workflow=False, reason="invalid_mode")

    normalized_status = str(workflow_status or "").strip().lower()
    if normalized_status in {WORKFLOW_STATUS_QUEUED, WORKFLOW_STATUS_RUNNING}:
        return AttemptCreationPolicy(allowed=False, reuse_workflow=False, reason="active_attempt")
    if normalized_status == WORKFLOW_STATUS_WAITING_FOR_INPUT:
        if normalized_mode == "resume":
            return AttemptCreationPolicy(allowed=True, reuse_workflow=True)
        return AttemptCreationPolicy(allowed=False, reuse_workflow=False, reason="waiting_for_input_requires_resume")

    return AttemptCreationPolicy(allowed=True, reuse_workflow=False)
