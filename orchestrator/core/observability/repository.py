from __future__ import annotations

from datetime import datetime, timezone
import base64
import json
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from orchestrator.core.config import get_settings
from orchestrator.core.observability.models import EventClass, EventCursor, ProductEvent


class ProductEventRepository(Protocol):
    def initialize(self) -> None: ...

    def insert_event(self, row: ProductEvent) -> None: ...

    def list_events(
        self,
        *,
        event_class: EventClass,
        filters: dict[str, str | None],
        limit: int,
        before: EventCursor | None = None,
        newest_first: bool = True,
    ) -> list[ProductEvent]: ...

    def list_events_after_sequence(
        self,
        *,
        event_class: EventClass,
        filters: dict[str, str | None],
        after_sequence: int,
        limit: int,
    ) -> list[ProductEvent]: ...


class ClickHouseProductEventRepository:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = str(getattr(settings, "clickhouse_http_url", "") or "").rstrip(
            "/"
        )
        self._database = str(getattr(settings, "clickhouse_database", "") or "").strip()
        self._username = str(getattr(settings, "clickhouse_username", "") or "").strip()
        self._password = str(getattr(settings, "clickhouse_password", "") or "")
        self._timeout = max(
            1, int(getattr(settings, "clickhouse_query_timeout_seconds", 30))
        )
        if not self._base_url or not self._database:
            raise RuntimeError(
                "ClickHouse product event repository requires clickhouse_http_url and clickhouse_database"
            )

    def initialize(self) -> None:
        self._execute(
            "CREATE DATABASE IF NOT EXISTS " + _quote_identifier(self._database),
            database="",
        )
        self._execute(_create_execution_log_table_sql())
        self._execute(_create_audit_evidence_table_sql())

    def insert_event(self, row: ProductEvent) -> None:
        self._execute(_insert_sql(table_name=_table_name(row.event_class), row=row))

    def list_events(
        self,
        *,
        event_class: EventClass,
        filters: dict[str, str | None],
        limit: int,
        before: EventCursor | None = None,
        newest_first: bool = True,
    ) -> list[ProductEvent]:
        order = "DESC" if newest_first else "ASC"
        sql = f"""
            SELECT
                event_sequence, event_id, event_class, tenant_id, project_id, workflow_id, run_id,
                operation_id, attempt_id, issue_key, event_kind, level, source_component, message,
                payload_json, recorded_at
            FROM {_table_name(event_class)}
            {_where_clause(filters, before=before)}
            ORDER BY recorded_at {order}, event_sequence {order}
            LIMIT {_limit(limit)}
        """
        return self._query_events(sql)

    def list_events_after_sequence(
        self,
        *,
        event_class: EventClass,
        filters: dict[str, str | None],
        after_sequence: int,
        limit: int,
    ) -> list[ProductEvent]:
        sql = f"""
            SELECT
                event_sequence, event_id, event_class, tenant_id, project_id, workflow_id, run_id,
                operation_id, attempt_id, issue_key, event_kind, level, source_component, message,
                payload_json, recorded_at
            FROM {_table_name(event_class)}
            {_where_clause(filters, after_sequence=after_sequence)}
            ORDER BY event_sequence ASC
            LIMIT {_limit(limit)}
        """
        return self._query_events(sql)

    def _query_events(self, sql: str) -> list[ProductEvent]:
        raw = self._execute(sql.rstrip(";") + " FORMAT JSONEachRow")
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return [event_from_json(row) for row in rows]

    def _execute(self, sql: str, *, database: str | None = None) -> str:
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
            token = base64.b64encode(
                f"{self._username}:{self._password}".encode("utf-8")
            ).decode("ascii")
            request.add_header("Authorization", f"Basic {token}")
        try:
            with urlopen(request, timeout=self._timeout) as response:
                return response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"ClickHouse query failed: {exc.code} {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"ClickHouse is unavailable: {exc.reason}") from exc


_repository: ProductEventRepository | None = None


def default_product_event_repository() -> ProductEventRepository:
    global _repository
    if _repository is None:
        _repository = ClickHouseProductEventRepository()
    return _repository


def configure_product_event_repository_for_tests(
    repository: ProductEventRepository,
) -> None:
    global _repository
    _repository = repository


def reset_product_event_repository_for_tests() -> None:
    global _repository
    _repository = None


def event_from_json(row: dict[str, object]) -> ProductEvent:
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _table_name(event_class: EventClass) -> str:
    return (
        "audit_evidence_events"
        if event_class == "audit_evidence"
        else "execution_log_events"
    )


def _quote_identifier(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized.replace("_", "").isalnum():
        raise ValueError(f"Invalid ClickHouse identifier: {normalized}")
    return f"`{normalized}`"


def _quote_sql(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, datetime):
        value = _as_utc(value).strftime("%Y-%m-%d %H:%M:%S.%f")
    text = str(value)
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _limit(limit: int) -> int:
    return max(1, min(int(limit), 2000))


def _where_clause(
    filters: dict[str, str | None],
    *,
    before: EventCursor | None = None,
    after_sequence: int | None = None,
) -> str:
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


def _insert_sql(*, table_name: str, row: ProductEvent) -> str:
    return f"""
        INSERT INTO {table_name} (
            event_sequence, event_id, event_class, tenant_id, project_id, workflow_id, run_id, operation_id, attempt_id,
            issue_key, event_kind, level, source_component, message, payload_json, recorded_at
        ) VALUES (
            {int(row.event_sequence)}, {_quote_sql(row.event_id)}, {_quote_sql(row.event_class)}, {_quote_sql(row.tenant_id)},
            {_quote_sql(row.project_id)}, {_quote_sql(row.workflow_id)}, {_quote_sql(row.run_id)},
            {_quote_sql(row.operation_id)}, {_quote_sql(row.attempt_id)}, {_quote_sql(row.issue_key)},
            {_quote_sql(row.event_kind)}, {_quote_sql(row.level)}, {_quote_sql(row.source_component)},
            {_quote_sql(row.message)}, {_quote_sql(json.dumps(row.payload_json, separators=(",", ":"), sort_keys=True))},
            {_quote_sql(row.recorded_at)}
        )
    """


def _create_execution_log_table_sql() -> str:
    return """
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


def _create_audit_evidence_table_sql() -> str:
    return """
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
