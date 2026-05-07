from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from orchestrator.core.observability.models import EventClass, EventCursor, ProductEvent
from orchestrator.core.observability import repository as repository_module
from orchestrator.core.observability.writer import PostgresProductEventNotifier, ProductEventWriter


def initialize_product_event_store() -> None:
    repository_module.default_product_event_repository().initialize()


def reset_event_store_for_tests() -> None:
    repository_module.reset_product_event_repository_for_tests()


def record_product_event(
    session: Session,
    *,
    event_class: EventClass,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None,
    run_id: str | None,
    operation_id: str | None,
    attempt_id: str | None,
    issue_key: str | None,
    event_kind: str,
    level: str,
    source_component: str | None,
    message: str,
    payload: dict | None = None,
    recorded_at: datetime | None = None,
    event_id: str | None = None,
    writer: ProductEventWriter | None = None,
) -> ProductEvent:
    resolved_writer = writer or ProductEventWriter(
        repository=repository_module.default_product_event_repository(),
        notifier=PostgresProductEventNotifier(),
    )
    return resolved_writer.record(
        session,
        event_class=event_class,
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id=workflow_id,
        run_id=run_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        issue_key=issue_key,
        event_kind=event_kind,
        level=level,
        source_component=source_component,
        message=message,
        payload=payload,
        recorded_at=recorded_at,
        event_id=event_id,
    )


def list_product_events(
    *,
    event_class: EventClass,
    filters: dict[str, str | None],
    limit: int,
    before: EventCursor | None = None,
    newest_first: bool = True,
) -> list[ProductEvent]:
    return repository_module.default_product_event_repository().list_events(
        event_class=event_class,
        filters=filters,
        limit=limit,
        before=before,
        newest_first=newest_first,
    )


def list_product_events_after_sequence(
    *,
    event_class: EventClass,
    filters: dict[str, str | None],
    after_sequence: int,
    limit: int,
) -> list[ProductEvent]:
    return repository_module.default_product_event_repository().list_events_after_sequence(
        event_class=event_class,
        filters=filters,
        after_sequence=after_sequence,
        limit=limit,
    )
