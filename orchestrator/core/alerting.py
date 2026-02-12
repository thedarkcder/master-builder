from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class AlertCandidate:
    alert_key: str
    severity: str
    scope_type: str
    scope_id: str | None
    reason: str


class AlertDedupRegistry:
    def __init__(self) -> None:
        self._active_last_emitted: dict[str, datetime] = {}

    def reset(self) -> None:
        self._active_last_emitted.clear()

    def filter_candidates(
        self,
        *,
        candidates: list[AlertCandidate],
        now: datetime,
        cooldown_seconds: int,
    ) -> list[AlertCandidate]:
        next_keys = {candidate.alert_key for candidate in candidates}
        stale_keys = [key for key in self._active_last_emitted if key not in next_keys]
        for key in stale_keys:
            self._active_last_emitted.pop(key, None)

        emitted: list[AlertCandidate] = []
        cooldown = timedelta(seconds=max(1, cooldown_seconds))
        for candidate in candidates:
            last_emitted = self._active_last_emitted.get(candidate.alert_key)
            if last_emitted is None or (now - last_emitted) >= cooldown:
                emitted.append(candidate)
                self._active_last_emitted[candidate.alert_key] = now
        return emitted


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


alert_dedup_registry = AlertDedupRegistry()


def reset_alert_dedup_registry_for_tests() -> None:
    alert_dedup_registry.reset()

