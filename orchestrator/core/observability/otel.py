from __future__ import annotations

import contextvars
import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from orchestrator.core.observability.otel_telemetry import current_trace_context

REQUIRED_LOG_FIELDS = (
    "timestamp",
    "level",
    "environment",
    "platform_version",
    "tenant_id",
    "project_id",
    "agent_id",
    "correlation_id",
    "trace_id",
    "span_id",
    "event_type",
    "message",
    "metadata",
)

_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "observability_correlation_id",
    default=None,
)
_tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "observability_tenant_id",
    default=None,
)
_project_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "observability_project_id",
    default=None,
)
_agent_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "observability_agent_id",
    default=None,
)


def set_log_context(
    *,
    correlation_id: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
) -> dict[str, contextvars.Token]:
    tokens: dict[str, contextvars.Token] = {}
    if correlation_id is not None:
        tokens["correlation_id"] = _correlation_id_var.set(correlation_id)
    if tenant_id is not None:
        tokens["tenant_id"] = _tenant_id_var.set(tenant_id)
    if project_id is not None:
        tokens["project_id"] = _project_id_var.set(project_id)
    if agent_id is not None:
        tokens["agent_id"] = _agent_id_var.set(agent_id)
    return tokens


def reset_log_context(tokens: dict[str, contextvars.Token]) -> None:
    if (token := tokens.get("correlation_id")) is not None:
        _correlation_id_var.reset(token)
    if (token := tokens.get("tenant_id")) is not None:
        _tenant_id_var.reset(token)
    if (token := tokens.get("project_id")) is not None:
        _project_id_var.reset(token)
    if (token := tokens.get("agent_id")) is not None:
        _agent_id_var.reset(token)


def current_log_context() -> dict[str, str | None]:
    return {
        "correlation_id": _correlation_id_var.get(),
        "tenant_id": _tenant_id_var.get(),
        "project_id": _project_id_var.get(),
        "agent_id": _agent_id_var.get(),
    }


def validate_log_payload(payload: dict[str, Any]) -> list[str]:
    return [field for field in REQUIRED_LOG_FIELDS if field not in payload]


@contextmanager
def scoped_log_context(
    *,
    correlation_id: str | None = None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    agent_id: str | None = None,
):
    tokens = set_log_context(
        correlation_id=correlation_id,
        tenant_id=tenant_id,
        project_id=project_id,
        agent_id=agent_id,
    )
    try:
        yield
    finally:
        reset_log_context(tokens)


class ObservabilityJsonFormatter(logging.Formatter):
    def __init__(
        self, *, environment: str, platform_version: str, default_agent_id: str = ""
    ) -> None:
        super().__init__()
        self._environment = environment
        self._platform_version = platform_version
        self._default_agent_id = _normalize_log_field(default_agent_id)

    def format(self, record: logging.LogRecord) -> str:
        context = current_log_context()
        trace_context = current_trace_context()
        metadata = dict(getattr(record, "metadata", {}) or {})
        metadata.setdefault("logger", record.name)
        metadata.setdefault("module", record.module)
        metadata.setdefault("line", record.lineno)
        if record.exc_info:
            metadata.setdefault("exception", self.formatException(record.exc_info))

        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "environment": self._environment,
            "platform_version": self._platform_version,
            "tenant_id": _normalize_log_field(
                getattr(record, "tenant_id", context["tenant_id"])
            ),
            "project_id": _normalize_log_field(
                getattr(record, "project_id", context["project_id"])
            ),
            "agent_id": _normalize_log_field(
                getattr(record, "agent_id", context["agent_id"]),
                default=self._default_agent_id,
            ),
            "correlation_id": _normalize_log_field(
                getattr(record, "correlation_id", context["correlation_id"])
            ),
            "trace_id": _normalize_log_field(
                getattr(record, "trace_id", trace_context["trace_id"])
            ),
            "span_id": _normalize_log_field(
                getattr(record, "span_id", trace_context["span_id"])
            ),
            "event_type": _normalize_log_field(
                getattr(record, "event_type", record.name), default=record.name
            ),
            "message": record.getMessage(),
            "metadata": metadata,
        }
        missing = validate_log_payload(payload)
        if missing:
            payload["metadata"]["missing_fields"] = missing
        return json.dumps(payload, sort_keys=True, ensure_ascii=True)


def _normalize_log_field(value: object, *, default: str = "") -> str:
    normalized = str(value or "").strip()
    if normalized:
        return normalized
    return str(default or "").strip()
