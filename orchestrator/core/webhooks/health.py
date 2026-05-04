from __future__ import annotations

import threading
from collections import defaultdict


class WebhookHealthTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], int] = defaultdict(int)

    def reset(self) -> None:
        with self._lock:
            self._events.clear()

    def record(self, *, tenant_id: str, outcome: str) -> None:
        normalized_tenant_id = _normalize_tenant_id(tenant_id)
        normalized_outcome = _normalize_outcome(outcome)
        with self._lock:
            self._events[(normalized_tenant_id, normalized_outcome)] += 1

    def rollup(self, *, tenant_id: str) -> dict[str, float]:
        normalized_tenant_id = _normalize_tenant_id(tenant_id)
        with self._lock:
            received = float(self._events.get((normalized_tenant_id, "received"), 0))
            failed = float(self._events.get((normalized_tenant_id, "failed"), 0))
        failure_rate = 0.0 if received <= 0.0 else failed / received
        return {
            "received_total": received,
            "failed_total": failed,
            "failure_rate_ratio": round(max(0.0, failure_rate), 6),
        }


def _normalize_tenant_id(value: str) -> str:
    normalized = str(value or "").strip()
    return normalized or "unknown"


def _normalize_outcome(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"received", "failed"}:
        return normalized
    return "failed"


webhook_health_tracker = WebhookHealthTracker()


def reset_webhook_health_tracker_for_tests() -> None:
    webhook_health_tracker.reset()

