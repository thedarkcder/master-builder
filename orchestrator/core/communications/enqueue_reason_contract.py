from __future__ import annotations

from typing import Protocol


class _RunLike(Protocol):
    run_id: str
    status: str


_ENQUEUE_REASON_GUIDANCE = {
    "run_already_active": "A run for this issue is already active.",
    "tenant_concurrency_limit_reached": "The tenant concurrency limit is reached; wait for an active run to finish.",
    "duplicate_delivery": "This webhook delivery was already processed.",
    "project_not_mapped": "Issue key is not mapped to an active project.",
    "no_retryable_run": "No failed/blocked/cancelled run is available to retry for this issue.",
}


def enqueue_reason_guidance(reason: str) -> str:
    return _ENQUEUE_REASON_GUIDANCE.get(
        reason,
        "Run was not queued due to current execution policy.",
    )


def format_enqueue_conflict_detail(
    *,
    prefix: str,
    enqueue_reason: str,
    enqueue_run_obj: _RunLike | None,
) -> str:
    guidance = enqueue_reason_guidance(enqueue_reason)
    if enqueue_reason == "run_already_active" and enqueue_run_obj is not None:
        return (
            f"{prefix}: {enqueue_reason} "
            f"(active run: {enqueue_run_obj.run_id}, status: {enqueue_run_obj.status}). "
            f"{guidance}"
        )
    return f"{prefix}: {enqueue_reason}. {guidance}"

