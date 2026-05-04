from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

EventClass = Literal["execution_log", "audit_evidence", "system_log"]


@dataclass(frozen=True)
class ProductEvent:
    event_sequence: int
    event_id: str
    event_class: EventClass
    tenant_id: str
    project_id: str | None
    workflow_id: str | None
    run_id: str | None
    operation_id: str | None
    attempt_id: str | None
    issue_key: str | None
    event_kind: str
    level: str
    source_component: str | None
    message: str
    payload_json: dict[str, object]
    recorded_at: datetime

    @property
    def stream_offset(self) -> int:
        return self.event_sequence


@dataclass(frozen=True)
class EventCursor:
    recorded_at: datetime | None = None
    event_sequence: int | None = None
