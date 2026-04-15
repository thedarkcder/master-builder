from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.core.run_enqueue_types import EnqueueFailureReason


class _RunLike(Protocol):
    run_id: str
    status: str


@dataclass(frozen=True)
class DiscordEnqueueConflictPresentation:
    detail: str


_DISCORD_ENQUEUE_FAILURE_GUIDANCE: dict[EnqueueFailureReason, str] = {
    EnqueueFailureReason.DUPLICATE_DELIVERY: "This webhook delivery was already processed.",
    EnqueueFailureReason.RUN_ALREADY_ACTIVE: "A run for this issue is already active.",
    EnqueueFailureReason.TENANT_CONCURRENCY_LIMIT_REACHED: (
        "The tenant concurrency limit is reached; wait for an active run to finish."
    ),
}


def present_discord_enqueue_conflict(
    *,
    prefix: str,
    reason: EnqueueFailureReason,
    enqueue_run_obj: _RunLike | None,
) -> DiscordEnqueueConflictPresentation:
    guidance = _DISCORD_ENQUEUE_FAILURE_GUIDANCE.get(
        reason,
        "Run was not queued due to current execution policy.",
    )
    if reason is EnqueueFailureReason.RUN_ALREADY_ACTIVE and enqueue_run_obj is not None:
        return DiscordEnqueueConflictPresentation(
            detail=(
                f"{prefix}: {reason.value} "
                f"(active run: {enqueue_run_obj.run_id}, status: {enqueue_run_obj.status}). "
                f"{guidance}"
            )
        )
    return DiscordEnqueueConflictPresentation(detail=f"{prefix}: {reason.value}. {guidance}")
