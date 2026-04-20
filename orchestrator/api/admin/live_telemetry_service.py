from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from hashlib import sha1
from typing import Any
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from orchestrator.api.admin.schema_mappers import workflow_observability_event_to_schema
from orchestrator.api.schemas import WorkflowObservabilityEventRead
from orchestrator.core.config import Settings

_LOKI_QUERY_TIMEOUT_SECONDS = 10
_DEFAULT_LOOKBACK = timedelta(hours=24)
_LIVE_TELEMETRY_SERVICE_NAME_REGEX = "api|run-worker|webhook-worker|temporal-worker|project-automation|knowledge-sync"
_MIN_LOKI_QUERY_LIMIT = 500
_MAX_LOKI_QUERY_LIMIT = 5000
_RESERVED_STREAM_KEYS = {
    "service_name",
    "service_namespace",
    "deployment_environment",
    "severity_number",
    "severity_text",
    "detected_level",
    "scope_name",
    "observed_timestamp",
    "telemetry_sdk_language",
    "telemetry_sdk_name",
    "telemetry_sdk_version",
}


def list_live_workflow_telemetry_events(
    *,
    settings: Settings,
    tenant_id: str,
    workflow_id: str,
    operation_id: str | None = None,
    limit: int = 200,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> list[WorkflowObservabilityEventRead]:
    base_url = str(getattr(settings, "observability_loki_query_base_url", "") or "").strip().rstrip("/")
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_workflow_id = str(workflow_id or "").strip()
    normalized_operation_id = str(operation_id or "").strip() or None
    if not base_url or not normalized_tenant_id or not normalized_workflow_id:
        return []

    query = _service_logql_query()
    params = {
        "query": query,
        "limit": str(_loki_query_limit(limit)),
        "direction": "BACKWARD",
        "start": _ns_epoch(start_at or (datetime.now(timezone.utc) - _DEFAULT_LOOKBACK)),
        "end": _ns_epoch(end_at or datetime.now(timezone.utc)),
    }
    request = urllib_request.Request(
        f"{base_url}/loki/api/v1/query_range?{urllib_parse.urlencode(params)}",
        headers={"Accept": "application/json"},
        method="GET",
    )
    with urllib_request.urlopen(request, timeout=_LOKI_QUERY_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return _workflow_observability_events_from_loki(
        payload,
        tenant_id=normalized_tenant_id,
        workflow_id=normalized_workflow_id,
        operation_id=normalized_operation_id,
    )


def _service_logql_query() -> str:
    return "{service_name=~\"" + _LIVE_TELEMETRY_SERVICE_NAME_REGEX + "\"}"


def _escape_logql_value(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _loki_query_limit(limit: int) -> int:
    normalized_limit = max(1, int(limit or 0))
    return max(_MIN_LOKI_QUERY_LIMIT, min(normalized_limit * 20, _MAX_LOKI_QUERY_LIMIT))


def _ns_epoch(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return str(int(normalized.timestamp() * 1_000_000_000))


def _workflow_observability_events_from_loki(
    payload: dict[str, Any],
    *,
    tenant_id: str,
    workflow_id: str,
    operation_id: str | None,
) -> list[WorkflowObservabilityEventRead]:
    rows = payload.get("data", {}).get("result", [])
    if not isinstance(rows, list):
        return []
    events: list[WorkflowObservabilityEventRead] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        stream = row.get("stream")
        values = row.get("values")
        if not isinstance(stream, dict) or not isinstance(values, list):
            continue
        stream_tenant_id = _stream_value(stream, "tenant_id")
        if stream_tenant_id != tenant_id:
            continue
        stream_workflow_id = _stream_value(stream, "metadata_workflow_id")
        if stream_workflow_id != workflow_id:
            continue
        stream_operation_id = _stream_value(stream, "metadata_operation_id")
        if operation_id is not None and stream_operation_id != operation_id:
            continue
        for index, item in enumerate(values):
            if not isinstance(item, list) or len(item) < 2:
                continue
            timestamp_ns = str(item[0] or "").strip()
            message = str(item[1] or "").strip()
            if not timestamp_ns or not message:
                continue
            event_payload = _payload_from_stream(stream)
            event_kind = str(event_payload.get("event_kind") or stream.get("event_type") or "runtime_log").strip().lower()
            events.append(
                workflow_observability_event_to_schema(
                    {
                        "event_id": _loki_event_id(timestamp_ns=timestamp_ns, stream=stream, message=message, index=index),
                        "source": "telemetry",
                        "level": str(
                            stream.get("detected_level")
                            or stream.get("severity_text")
                            or "info"
                        ).strip().lower(),
                        "event_kind": event_kind,
                        "message": message,
                        "source_component": str(stream.get("scope_name") or stream.get("service_name") or "").strip() or None,
                        "run_id": _stream_value(stream, "metadata_run_id"),
                        "operation_id": _stream_value(stream, "metadata_operation_id") or operation_id,
                        "attempt_id": _stream_value(stream, "metadata_attempt_id"),
                        "agent_id": _stream_value(stream, "agent_id") or _stream_value(stream, "metadata_agent_id"),
                        "invocation_id": _stream_value(stream, "metadata_invocation_id"),
                        "stage": _stream_value(stream, "metadata_stage"),
                        "attempt": _int_stream_value(stream, "metadata_attempt_number"),
                        "stream": _stream_value(stream, "metadata_stream"),
                        "payload": event_payload,
                        "recorded_at": _datetime_from_ns(timestamp_ns),
                    }
                )
            )
    events.sort(key=lambda item: (item.recorded_at, item.event_id), reverse=True)
    return events


def _payload_from_stream(stream: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in stream.items():
        normalized_key = str(key or "").strip()
        if not normalized_key or normalized_key in _RESERVED_STREAM_KEYS:
            continue
        if normalized_key.startswith("metadata_"):
            payload[normalized_key.removeprefix("metadata_")] = value
            continue
        if normalized_key.startswith("code_"):
            payload[normalized_key] = value
    return payload


def _stream_value(stream: dict[str, Any], key: str) -> str | None:
    value = str(stream.get(key) or "").strip()
    return value or None


def _int_stream_value(stream: dict[str, Any], key: str) -> int | None:
    value = _stream_value(stream, key)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _datetime_from_ns(value: str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1_000_000_000, tz=timezone.utc)


def _loki_event_id(*, timestamp_ns: str, stream: dict[str, Any], message: str, index: int) -> str:
    digest = sha1(
        f"{timestamp_ns}|{stream.get('service_name')}|{stream.get('scope_name')}|{message}|{index}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()
    return f"telemetry:{digest}"
