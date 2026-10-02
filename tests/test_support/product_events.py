from __future__ import annotations

from orchestrator.core.observability.models import EventClass, EventCursor, ProductEvent


class RecordingProductEventRepository:
    def __init__(self, rows: list[ProductEvent] | None = None) -> None:
        self.inserted: list[ProductEvent] = []
        self.rows = list(rows or [])
        self.initialized = False

    def initialize(self) -> None:
        self.initialized = True

    def insert_event(self, row: ProductEvent) -> None:
        self.inserted.append(row)

    def list_events(
        self,
        *,
        event_class: EventClass,
        filters: dict[str, str | None],
        limit: int,
        before: EventCursor | None = None,
        newest_first: bool = True,
    ) -> list[ProductEvent]:
        del before
        rows = [
            row
            for row in [*self.rows, *self.inserted]
            if row.event_class == event_class and _matches_filters(row, filters)
        ]
        rows = sorted(
            rows,
            key=lambda row: (row.recorded_at, row.event_sequence),
            reverse=newest_first,
        )
        return rows[:limit]

    def list_events_after_sequence(
        self,
        *,
        event_class: EventClass,
        filters: dict[str, str | None],
        after_sequence: int,
        limit: int,
    ) -> list[ProductEvent]:
        rows = [
            row
            for row in [*self.rows, *self.inserted]
            if row.event_class == event_class
            and row.event_sequence > after_sequence
            and _matches_filters(row, filters)
        ]
        rows = sorted(rows, key=lambda row: row.event_sequence)
        return rows[:limit]


def _matches_filters(row: ProductEvent, filters: dict[str, str | None]) -> bool:
    for key, expected in filters.items():
        if expected is None:
            continue
        if str(getattr(row, key) or "") != str(expected):
            return False
    return True
