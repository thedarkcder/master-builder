from __future__ import annotations

from contextlib import contextmanager
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI
    from orchestrator.core.config import Settings

logger = logging.getLogger(__name__)

_initialized_services: set[str] = set()
_tracer_provider = None


def _otel_signal_endpoint(base_endpoint: str, signal: str) -> str:
    normalized = str(base_endpoint or "").strip().rstrip("/")
    if not normalized:
        return ""
    if normalized.endswith(f"/v1/{signal}"):
        return normalized
    return f"{normalized}/v1/{signal}"


def _parse_otlp_headers(raw_headers: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in str(raw_headers or "").split(","):
        normalized = item.strip()
        if not normalized or "=" not in normalized:
            continue
        key, value = normalized.split("=", 1)
        header_key = str(key or "").strip()
        header_value = str(value or "").strip()
        if header_key and header_value:
            parsed[header_key] = header_value
    return parsed


def initialize_telemetry(
    *,
    settings: "Settings",
    service_name: str,
    app: "FastAPI | None" = None,
) -> bool:
    global _tracer_provider

    if not bool(getattr(settings, "otel_enabled", False)):
        return False

    normalized_service_name = str(service_name or "").strip()
    if not normalized_service_name:
        raise ValueError("service_name is required for telemetry initialization")

    if normalized_service_name in _initialized_services:
        if app is not None:
            _instrument_fastapi_app(app=app)
        return True

    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        from opentelemetry.trace import set_tracer_provider
    except ImportError:
        logger.warning(
            "telemetry_initialization_skipped_missing_dependency service_name=%s",
            normalized_service_name,
        )
        return False

    base_endpoint = str(getattr(settings, "otel_exporter_otlp_endpoint", "") or "").strip()
    if not base_endpoint:
        logger.warning(
            "telemetry_initialization_skipped_missing_endpoint service_name=%s",
            normalized_service_name,
        )
        return False

    resource = Resource.create(
        {
            "service.name": normalized_service_name,
            "service.namespace": str(getattr(settings, "otel_service_namespace", "") or "").strip()
            or "master-builder",
            "deployment.environment": str(getattr(settings, "sentry_environment", "") or "").strip()
            or "dev",
        }
    )
    headers = _parse_otlp_headers(str(getattr(settings, "otel_exporter_otlp_headers", "") or ""))
    sample_ratio = float(getattr(settings, "otel_traces_sample_ratio", 1.0) or 1.0)
    sample_ratio = min(max(sample_ratio, 0.0), 1.0)

    tracer_provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(root=TraceIdRatioBased(sample_ratio)),
    )
    tracer_provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=_otel_signal_endpoint(base_endpoint, "traces"),
                headers=headers,
            )
        )
    )
    set_tracer_provider(tracer_provider)
    _tracer_provider = tracer_provider

    _initialized_services.add(normalized_service_name)

    if app is not None:
        _instrument_fastapi_app(app=app)
    return True


def _instrument_fastapi_app(*, app: "FastAPI") -> None:
    if getattr(app.state, "otel_instrumented", False):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.trace import Status, StatusCode
    except ImportError:
        return
    if _tracer_provider is None:
        return

    tracer = trace.get_tracer("orchestrator.api.fastapi")

    @app.middleware("http")
    async def _otel_request_span_middleware(request: Any, call_next: Any) -> Any:
        path = str(getattr(request.url, "path", "") or "")
        if path in {"/health", "/metrics"}:
            return await call_next(request)

        method = str(getattr(request, "method", "") or "HTTP")
        span_name = f"{method} {path}"
        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("http.request.method", method)
            span.set_attribute("url.path", path)
            try:
                response = await call_next(request)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            span.set_attribute("http.response.status_code", int(getattr(response, "status_code", 0) or 0))
            if int(getattr(response, "status_code", 0) or 0) >= 500:
                span.set_status(Status(StatusCode.ERROR))
            return response

    app.state.otel_instrumented = True


def shutdown_telemetry() -> None:
    global _tracer_provider

    if _tracer_provider is not None:
        try:
            _tracer_provider.shutdown()
        except Exception:
            logger.exception("telemetry_tracer_provider_shutdown_failed")
    _tracer_provider = None
    _initialized_services.clear()


def current_trace_context() -> dict[str, str | None]:
    try:
        from opentelemetry import trace
    except ImportError:
        return {"trace_id": None, "span_id": None}
    span = trace.get_current_span()
    if span is None:
        return {"trace_id": None, "span_id": None}
    context = span.get_span_context()
    if context is None or not getattr(context, "is_valid", False):
        return {"trace_id": None, "span_id": None}
    return {
        "trace_id": format(context.trace_id, "032x"),
        "span_id": format(context.span_id, "016x"),
    }


@contextmanager
def telemetry_span(name: str, *, attributes: dict[str, Any] | None = None):
    trace = None
    try:
        from opentelemetry import trace
    except ImportError:
        pass

    if trace is None:
        yield None
        return

    tracer = trace.get_tracer("orchestrator")
    with tracer.start_as_current_span(str(name or "operation")) as span:
        for key, value in (attributes or {}).items():
            normalized_key = str(key or "").strip()
            if not normalized_key or value is None:
                continue
            span.set_attribute(normalized_key, value)
        yield span
