from __future__ import annotations

from enum import Enum


class EnqueueFailureReason(str, Enum):
    DUPLICATE_DELIVERY = "duplicate_delivery"
    RUN_ALREADY_ACTIVE = "run_already_active"
    TENANT_CONCURRENCY_LIMIT_REACHED = "tenant_concurrency_limit_reached"
