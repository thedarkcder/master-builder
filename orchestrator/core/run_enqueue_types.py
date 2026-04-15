from __future__ import annotations

from enum import Enum


class EnqueueFailureReason(str, Enum):
    DUPLICATE_DELIVERY = "duplicate_delivery"
    RUN_ALREADY_ACTIVE = "run_already_active"
    TENANT_CONCURRENCY_LIMIT_REACHED = "tenant_concurrency_limit_reached"

    @property
    def guidance(self) -> str:
        if self is EnqueueFailureReason.DUPLICATE_DELIVERY:
            return "This webhook delivery was already processed."
        if self is EnqueueFailureReason.RUN_ALREADY_ACTIVE:
            return "A run for this issue is already active."
        if self is EnqueueFailureReason.TENANT_CONCURRENCY_LIMIT_REACHED:
            return "The tenant concurrency limit is reached; wait for an active run to finish."
        return "Run was not queued due to current execution policy."
