from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.storage.models import WorkflowOperationAttempt

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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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


def _quote_sql(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        value = _as_utc(value).strftime("%Y-%m-%d %H:%M:%S.%f")
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _event_from_json(row: dict[str, object]) -> ProductEvent:
    payload = row.get("payload_json")
    if isinstance(payload, str):
        parsed_payload = json.loads(payload) if payload else {}
    elif isinstance(payload, dict):
        parsed_payload = payload
    else:
        parsed_payload = {}
    recorded = row.get("recorded_at")
    if isinstance(recorded, str):
        recorded_at = _as_utc(datetime.fromisoformat(recorded.replace("Z", "+00:00")))
    elif isinstance(recorded, datetime):
        recorded_at = _as_utc(recorded)
    else:
        recorded_at = _utcnow()
    return ProductEvent(
        event_sequence=int(row.get("event_sequence") or 0),
        event_id=str(row.get("event_id") or ""),
        event_class=str(row.get("event_class") or "execution_log"),  # type: ignore[arg-type]
        tenant_id=str(row.get("tenant_id") or ""),
        project_id=str(row.get("project_id") or "").strip() or None,
        workflow_id=str(row.get("workflow_id") or "").strip() or None,
        run_id=str(row.get("run_id") or "").strip() or None,
        operation_id=str(row.get("operation_id") or "").strip() or None,
        attempt_id=str(row.get("attempt_id") or "").strip() or None,
        issue_key=str(row.get("issue_key") or "").strip() or None,
        event_kind=str(row.get("event_kind") or ""),
        level=str(row.get("level") or "info"),
        source_component=str(row.get("source_component") or "").strip() or None,
        message=str(row.get("message") or ""),
        payload_json=parsed_payload,
        recorded_at=recorded_at,
    )


class ClickHouseEventStore:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = str(getattr(settings, "clickhouse_http_url", "") or "").rstrip("/")
        self._database = str(getattr(settings, "clickhouse_database", "") or "").strip()
        self._username = str(getattr(settings, "clickhouse_username", "") or "").strip()
        self._password = str(getattr(settings, "clickhouse_password", "") or "")
        self._timeout = max(1, int(getattr(settings, "clickhouse_query_timeout_seconds", 30)))
        if not self._base_url or not self._database:
            raise RuntimeError("ClickHouse event store requires clickhouse_http_url and clickhouse_database")

    def execute(self, sql: str, *, database: str | None = None) -> str:
        selected_database = self._database if database is None else database
        params = {"database": selected_database} if selected_database else {}
        url = f"{self._base_url}/?{urlencode(params)}"
        request = Request(
            url,
            data=sql.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        if self._username:
            import base64

            token = base64.b64encode(f"{self._username}:{self._password}".encode("utf-8")).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ClickHouse query failed: {exc.code} {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"ClickHouse is unavailable: {exc.reason}") from exc

    def query_events(self, sql: str) -> list[ProductEvent]:
        raw = self.execute(sql.rstrip(";") + " FORMAT JSONEachRow")
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return [_event_from_json(row) for row in rows]


_store: ClickHouseEventStore | None = None


def event_store() -> ClickHouseEventStore:
    global _store
    if _store is None:
        _store = ClickHouseEventStore()
    return _store


def reset_event_store_for_tests() -> None:
    global _store
    _store = None


def initialize_product_event_store() -> None:
    store = event_store()
    store.execute("CREATE DATABASE IF NOT EXISTS " + _quote_identifier(get_settings().clickhouse_database), database="")
    store.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_log_events (
            event_sequence UInt64 DEFAULT (toUnixTimestamp64Micro(recorded_at) * 100000 + cityHash64(event_id) % 100000),
            event_id String,
            event_class LowCardinality(String),
            tenant_id String,
            project_id Nullable(String),
            workflow_id Nullable(String),
            run_id Nullable(String),
            operation_id Nullable(String),
            attempt_id Nullable(String),
            issue_key Nullable(String),
            event_kind LowCardinality(String),
            level LowCardinality(String),
            source_component Nullable(String),
            message String,
            payload_json String,
            recorded_at DateTime64(6, 'UTC')
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(recorded_at)
        ORDER BY (
            tenant_id,
            ifNull(run_id, ''),
            ifNull(workflow_id, ''),
            ifNull(operation_id, ''),
            ifNull(attempt_id, ''),
            recorded_at,
            event_sequence
        )
        """
    )
    store.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_evidence_events (
            event_sequence UInt64 DEFAULT (toUnixTimestamp64Micro(recorded_at) * 100000 + cityHash64(event_id) % 100000),
            event_id String,
            event_class LowCardinality(String),
            tenant_id String,
            project_id Nullable(String),
            workflow_id Nullable(String),
            run_id Nullable(String),
            operation_id Nullable(String),
            attempt_id Nullable(String),
            issue_key Nullable(String),
            event_kind LowCardinality(String),
            level LowCardinality(String),
            source_component Nullable(String),
            message String,
            payload_json String,
            recorded_at DateTime64(6, 'UTC')
        )
        ENGINE = MergeTree
        PARTITION BY toYYYYMM(recorded_at)
        ORDER BY (
            tenant_id,
            ifNull(workflow_id, ''),
            ifNull(operation_id, ''),
            ifNull(attempt_id, ''),
            recorded_at,
            event_sequence
        )
        """
    )


def _quote_identifier(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.replace("_", "").isalnum():
        raise ValueError(f"Invalid ClickHouse identifier: {normalized}")
    return f"`{normalized}`"


def _validate_attempt_ownership(*, session: Session, operation_id: str | None, attempt_id: str | None) -> None:
    if operation_id is None and attempt_id is None:
        return
    if operation_id is None or attempt_id is None:
        raise ValueError("Operation-scoped events require operation_id and attempt_id together")
    attempt = session.get(WorkflowOperationAttempt, attempt_id)
    if attempt is None or attempt.operation_id != operation_id:
        raise ValueError("Event attempt_id must belong to the supplied operation_id")


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
) -> ProductEvent:
    normalized_operation_id = str(operation_id or "").strip() or None
    normalized_attempt_id = str(attempt_id or "").strip() or None
    _validate_attempt_ownership(
        session=session,
        operation_id=normalized_operation_id,
        attempt_id=normalized_attempt_id,
    )
    normalized_event_id = str(event_id or "").strip()
    if not normalized_event_id:
        from uuid import uuid4

        normalized_event_id = uuid4().hex
    timestamp = _as_utc(recorded_at) if recorded_at is not None else _utcnow()
    normalized_payload = _normalize_payload(payload)
    normalized_message = redact_sensitive_text(str(message or "").strip())
    if not str(tenant_id or "").strip() or not normalized_message or not str(event_kind or "").strip():
        raise ValueError("Product events require tenant_id, event_kind, and message")
    row = ProductEvent(
        event_sequence=0,
        event_id=normalized_event_id,
        event_class=event_class,
        tenant_id=str(tenant_id or "").strip(),
        project_id=str(project_id or "").strip() or None,
        workflow_id=str(workflow_id or "").strip() or None,
        run_id=str(run_id or "").strip() or None,
        operation_id=normalized_operation_id,
        attempt_id=normalized_attempt_id,
        issue_key=str(issue_key or "").strip() or None,
        event_kind=str(event_kind or "").strip().lower(),
        level=str(level or "").strip().lower() or "info",
        source_component=str(source_component or "").strip() or None,
        message=normalized_message,
        payload_json=normalized_payload,
        recorded_at=timestamp,
    )
    table_name = "audit_evidence_events" if event_class == "audit_evidence" else "execution_log_events"
    event_store().execute(_insert_sql(table_name=table_name, row=row))
    return row


def _insert_sql(*, table_name: str, row: ProductEvent) -> str:
    return f"""
        INSERT INTO {table_name} (
            event_id, event_class, tenant_id, project_id, workflow_id, run_id, operation_id, attempt_id,
            issue_key, event_kind, level, source_component, message, payload_json, recorded_at
        ) VALUES (
            {_quote_sql(row.event_id)}, {_quote_sql(row.event_class)}, {_quote_sql(row.tenant_id)},
            {_quote_sql(row.project_id)}, {_quote_sql(row.workflow_id)}, {_quote_sql(row.run_id)},
            {_quote_sql(row.operation_id)}, {_quote_sql(row.attempt_id)}, {_quote_sql(row.issue_key)},
            {_quote_sql(row.event_kind)}, {_quote_sql(row.level)}, {_quote_sql(row.source_component)},
            {_quote_sql(row.message)}, {_quote_sql(json.dumps(row.payload_json, separators=(",", ":"), sort_keys=True))},
            {_quote_sql(row.recorded_at)}
        )
    """


def _where_clause(filters: dict[str, str | None], *, before: EventCursor | None = None, after_sequence: int | None = None) -> str:
    clauses: list[str] = []
    for column, value in filters.items():
        if value is not None:
            clauses.append(f"{column} = {_quote_sql(value)}")
    if before is not None and before.recorded_at is not None:
        if before.event_sequence is not None:
            clauses.append(
                "(recorded_at < "
                + _quote_sql(before.recorded_at)
                + " OR (recorded_at = "
                + _quote_sql(before.recorded_at)
                + f" AND event_sequence < {int(before.event_sequence)}))"
            )
        else:
            clauses.append(f"recorded_at < {_quote_sql(before.recorded_at)}")
    if after_sequence is not None:
        clauses.append(f"event_sequence > {int(after_sequence)}")
    return "WHERE " + " AND ".join(clauses) if clauses else ""


def list_product_events(
    *,
    event_class: EventClass,
    filters: dict[str, str | None],
    limit: int,
    before: EventCursor | None = None,
    newest_first: bool = True,
) -> list[ProductEvent]:
    table_name = "audit_evidence_events" if event_class == "audit_evidence" else "execution_log_events"
    order = "DESC" if newest_first else "ASC"
    sql = f"""
        SELECT
            event_sequence, event_id, event_class, tenant_id, project_id, workflow_id, run_id,
            operation_id, attempt_id, issue_key, event_kind, level, source_component, message,
            payload_json, recorded_at
        FROM {table_name}
        {_where_clause(filters, before=before)}
        ORDER BY recorded_at {order}, event_sequence {order}
        LIMIT {max(1, min(limit, 2000))}
    """
    return event_store().query_events(sql)


def stream_product_events(
    *,
    event_class: EventClass,
    filters: dict[str, str | None],
    start_after_sequence: int,
    batch_limit: int = 500,
) -> Iterator[list[ProductEvent]]:
    cursor = max(0, int(start_after_sequence))
    while True:
        rows = list_product_events_after_sequence(
            event_class=event_class,
            filters=filters,
            after_sequence=cursor,
            limit=batch_limit,
        )
        if rows:
            cursor = max(row.event_sequence for row in rows)
            yield rows
        else:
            yield []


def list_product_events_after_sequence(
    *,
    event_class: EventClass,
    filters: dict[str, str | None],
    after_sequence: int,
    limit: int,
) -> list[ProductEvent]:
    table_name = "audit_evidence_events" if event_class == "audit_evidence" else "execution_log_events"
    sql = f"""
        SELECT
            event_sequence, event_id, event_class, tenant_id, project_id, workflow_id, run_id,
            operation_id, attempt_id, issue_key, event_kind, level, source_component, message,
            payload_json, recorded_at
        FROM {table_name}
        {_where_clause(filters, after_sequence=after_sequence)}
        ORDER BY event_sequence ASC
        LIMIT {max(1, min(limit, 2000))}
    """
    return event_store().query_events(sql)
