from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import blake2b
from typing import Protocol

from sqlalchemy.orm import Session

from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.observability.models import EventClass, ProductEvent
from orchestrator.core.observability.notifications import (
    publish_product_event_notification,
)
from orchestrator.core.observability.repository import ProductEventRepository
from orchestrator.storage.models import WorkflowOperationAttempt


class ProductEventNotifier(Protocol):
    def publish(self, row: ProductEvent) -> None: ...


@dataclass(frozen=True)
class PostgresProductEventNotifier:
    def publish(self, row: ProductEvent) -> None:
        publish_product_event_notification(
            event_class=row.event_class,
            event_sequence=row.event_sequence,
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            run_id=row.run_id,
            operation_id=row.operation_id,
            attempt_id=row.attempt_id,
        )


@dataclass(frozen=True)
class ProductEventWriter:
    repository: ProductEventRepository
    notifier: ProductEventNotifier

    def record(
        self,
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
    ) -> ProductEvent:
        normalized_operation_id = str(operation_id or "").strip() or None
        normalized_attempt_id = str(attempt_id or "").strip() or None
        _validate_attempt_ownership(
            session=session,
            operation_id=normalized_operation_id,
            attempt_id=normalized_attempt_id,
        )
        normalized_event_id = str(event_id or "").strip() or None
        if normalized_event_id is None:
            from uuid import uuid4

            normalized_event_id = uuid4().hex
        timestamp = _as_utc(recorded_at) if recorded_at is not None else _utcnow()
        normalized_message = redact_sensitive_text(str(message or "").strip())
        normalized_event_kind = str(event_kind or "").strip().lower()
        normalized_tenant_id = str(tenant_id or "").strip()
        if (
            not normalized_tenant_id
            or not normalized_message
            or not normalized_event_kind
        ):
            raise ValueError(
                "Product events require tenant_id, event_kind, and message"
            )
        row = ProductEvent(
            event_sequence=_event_sequence(
                event_id=normalized_event_id, recorded_at=timestamp
            ),
            event_id=normalized_event_id,
            event_class=event_class,
            tenant_id=normalized_tenant_id,
            project_id=str(project_id or "").strip() or None,
            workflow_id=str(workflow_id or "").strip() or None,
            run_id=str(run_id or "").strip() or None,
            operation_id=normalized_operation_id,
            attempt_id=normalized_attempt_id,
            issue_key=str(issue_key or "").strip() or None,
            event_kind=normalized_event_kind,
            level=str(level or "").strip().lower() or "info",
            source_component=str(source_component or "").strip() or None,
            message=normalized_message,
            payload_json=_normalize_payload(payload),
            recorded_at=timestamp,
        )
        self.repository.insert_event(row)
        self.notifier.publish(row)
        return row


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_sequence(*, event_id: str, recorded_at: datetime) -> int:
    timestamp = int(_as_utc(recorded_at).timestamp() * 1_000_000)
    digest = blake2b(event_id.encode("utf-8"), digest_size=8).digest()
    suffix = int.from_bytes(digest, "big") % 100_000
    return timestamp * 100_000 + suffix


def _normalize_payload(payload: dict | None) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if isinstance(value, str):
            normalized[normalized_key] = redact_sensitive_text(value)
        elif isinstance(value, dict):
            normalized[normalized_key] = _normalize_payload(value)
        elif isinstance(value, list):
            normalized[normalized_key] = [
                redact_sensitive_text(item) if isinstance(item, str) else item
                for item in value
            ]
        else:
            normalized[normalized_key] = value
    return normalized


def _validate_attempt_ownership(
    *, session: Session, operation_id: str | None, attempt_id: str | None
) -> None:
    if operation_id is None and attempt_id is None:
        return
    if operation_id is None or attempt_id is None:
        raise ValueError(
            "Operation-scoped events require operation_id and attempt_id together"
        )
    attempt = session.get(WorkflowOperationAttempt, attempt_id)
    if attempt is None or attempt.operation_id != operation_id:
        raise ValueError("Event attempt_id must belong to the supplied operation_id")
