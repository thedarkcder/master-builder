from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from orchestrator.core.runs.enqueue_types import EnqueueFailureReason


class _RunLike(Protocol):
    run_id: str
    status: str


@dataclass(frozen=True)
class DiscordEnqueueConflictPresentation:
    detail: str


def present_discord_enqueue_conflict(
    *,
    prefix: str,
    reason: EnqueueFailureReason,
    enqueue_run_obj: _RunLike | None,
) -> DiscordEnqueueConflictPresentation:
    guidance = reason.guidance
    if (
        reason is EnqueueFailureReason.RUN_ALREADY_ACTIVE
        and enqueue_run_obj is not None
    ):
        return DiscordEnqueueConflictPresentation(
            detail=(
                f"{prefix}: {reason.value} "
                f"(active run: {enqueue_run_obj.run_id}, status: {enqueue_run_obj.status}). "
                f"{guidance}"
            )
        )
    return DiscordEnqueueConflictPresentation(
        detail=f"{prefix}: {reason.value}. {guidance}"
    )
