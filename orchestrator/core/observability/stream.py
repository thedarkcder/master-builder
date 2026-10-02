from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from orchestrator.core.observability.events import ProductEvent
from orchestrator.core.observability.notifications import (
    current_product_event_notification_marker,
    wait_for_product_event_notification,
)


ProductEventSnapshotLoader = Callable[[], Iterable[ProductEvent]]
ProductEventAfterCursorLoader = Callable[[int], list[ProductEvent]]
ProductEventEncoder = Callable[[ProductEvent], str | None]


@dataclass(frozen=True)
class ProductEventStream:
    current_marker: Callable[[], int]
    wait_for_marker: Callable[[int, float], int]

    def rows(
        self,
        *,
        snapshot_loader: ProductEventSnapshotLoader,
        after_cursor_loader: ProductEventAfterCursorLoader,
        encoder: ProductEventEncoder,
        after_event_sequence: int | None = None,
        batch_limit: int = 500,
        heartbeat_timeout_seconds: float = 25,
    ) -> Iterator[str]:
        """Stream product events without a snapshot/subscribe race.

        The notification marker must be captured before the snapshot. Rows committed
        during the snapshot are then caught by the after-cursor drain before waiting.
        """
        cursor = max(0, int(after_event_sequence or 0))
        notification_marker = self.current_marker()
        if cursor == 0:
            snapshot_rows = list(snapshot_loader())
            cursor = max((int(row.event_sequence) for row in snapshot_rows), default=0)
            for row in snapshot_rows:
                payload = encoder(row)
                if payload is not None:
                    yield payload

        while True:
            while True:
                rows = after_cursor_loader(cursor)
                for row in rows:
                    payload = encoder(row)
                    if payload is not None:
                        yield payload
                if rows:
                    cursor = max(int(row.event_sequence) for row in rows)
                if len(rows) < batch_limit:
                    break

            next_marker = self.wait_for_marker(
                notification_marker, heartbeat_timeout_seconds
            )
            if next_marker <= notification_marker:
                yield "\n"
                continue
            notification_marker = next_marker


def stream_product_event_rows(
    *,
    snapshot_loader: ProductEventSnapshotLoader,
    after_cursor_loader: ProductEventAfterCursorLoader,
    encoder: ProductEventEncoder,
    after_event_sequence: int | None = None,
    batch_limit: int = 500,
    heartbeat_timeout_seconds: float = 25,
) -> Iterator[str]:
    stream = ProductEventStream(
        current_marker=current_product_event_notification_marker,
        wait_for_marker=lambda marker, timeout: wait_for_product_event_notification(
            marker=marker,
            timeout_seconds=timeout,
        ),
    )
    yield from stream.rows(
        snapshot_loader=snapshot_loader,
        after_cursor_loader=after_cursor_loader,
        encoder=encoder,
        after_event_sequence=after_event_sequence,
        batch_limit=batch_limit,
        heartbeat_timeout_seconds=heartbeat_timeout_seconds,
    )
