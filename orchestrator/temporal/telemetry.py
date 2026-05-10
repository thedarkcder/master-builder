from __future__ import annotations

from typing import Any

from temporalio import activity
from temporalio.client import Interceptor as ClientInterceptor
from temporalio.client import OutboundInterceptor
from temporalio.worker import ActivityInboundInterceptor
from temporalio.worker import Interceptor as WorkerInterceptor

from orchestrator.core.observability.otel_telemetry import telemetry_span


def _app_workflow_id(handle_id: str | None) -> str | None:
    normalized = str(handle_id or "").strip()
    if not normalized:
        return None
    if normalized.startswith("workflow:"):
        return normalized.removeprefix("workflow:") or None
    return normalized


def _client_workflow_attributes(*, operation: str, input: Any) -> dict[str, Any]:
    attributes: dict[str, Any] = {
        "temporal.operation": operation,
    }
    handle_id = str(getattr(input, "id", "") or "").strip() or None
    workflow_id = _app_workflow_id(handle_id)
    if handle_id:
        attributes["temporal.handle_id"] = handle_id
    if workflow_id:
        attributes["workflow.id"] = workflow_id
    run_id = str(getattr(input, "run_id", "") or "").strip() or None
    if run_id:
        attributes["temporal.workflow_run_id"] = run_id
    workflow_name = str(getattr(input, "workflow", "") or "").strip() or None
    if workflow_name:
        attributes["temporal.workflow_name"] = workflow_name
    task_queue = str(getattr(input, "task_queue", "") or "").strip() or None
    if task_queue:
        attributes["temporal.task_queue"] = task_queue
    update_name = str(getattr(input, "update", "") or "").strip() or None
    if update_name:
        attributes["temporal.update_name"] = update_name
    query_name = str(getattr(input, "query", "") or "").strip() or None
    if query_name:
        attributes["temporal.query_name"] = query_name
    signal_name = str(getattr(input, "signal", "") or "").strip() or None
    if signal_name:
        attributes["temporal.signal_name"] = signal_name
    return attributes


def _activity_attributes(input: Any) -> dict[str, Any]:
    info = activity.info()
    attributes: dict[str, Any] = {
        "temporal.activity_id": str(getattr(info, "activity_id", "") or "").strip(),
        "temporal.activity_type": str(getattr(info, "activity_type", "") or "").strip(),
        "temporal.activity_attempt": int(getattr(info, "attempt", 0) or 0),
        "temporal.workflow_id": str(getattr(info, "workflow_id", "") or "").strip(),
        "temporal.workflow_run_id": str(getattr(info, "workflow_run_id", "") or "").strip(),
        "temporal.workflow_type": str(getattr(info, "workflow_type", "") or "").strip(),
        "temporal.task_queue": str(getattr(info, "task_queue", "") or "").strip(),
    }
    workflow_id = _app_workflow_id(getattr(info, "workflow_id", None))
    if workflow_id:
        attributes["workflow.id"] = workflow_id
    fn = getattr(input, "fn", None)
    fn_name = getattr(fn, "__name__", "")
    if str(fn_name or "").strip():
        attributes["activity.name"] = str(fn_name).strip()
    return attributes


class TemporalClientTelemetryOutboundInterceptor(OutboundInterceptor):
    async def start_workflow(self, input):  # noqa: ANN001
        with telemetry_span(
            "temporal.client.start_workflow",
            attributes=_client_workflow_attributes(operation="start_workflow", input=input),
        ):
            return await super().start_workflow(input)

    async def signal_workflow(self, input):  # noqa: ANN001
        with telemetry_span(
            "temporal.client.signal_workflow",
            attributes=_client_workflow_attributes(operation="signal_workflow", input=input),
        ):
            return await super().signal_workflow(input)

    async def query_workflow(self, input):  # noqa: ANN001
        with telemetry_span(
            "temporal.client.query_workflow",
            attributes=_client_workflow_attributes(operation="query_workflow", input=input),
        ):
            return await super().query_workflow(input)

    async def start_workflow_update(self, input):  # noqa: ANN001
        with telemetry_span(
            "temporal.client.start_workflow_update",
            attributes=_client_workflow_attributes(operation="start_workflow_update", input=input),
        ):
            return await super().start_workflow_update(input)


class TemporalClientTelemetryInterceptor(ClientInterceptor):
    def intercept_client(self, next: OutboundInterceptor) -> OutboundInterceptor:
        return TemporalClientTelemetryOutboundInterceptor(next)


class TemporalActivityTelemetryInterceptor(ActivityInboundInterceptor):
    async def execute_activity(self, input):  # noqa: ANN001
        with telemetry_span(
            "temporal.activity.execute",
            attributes=_activity_attributes(input),
        ):
            return await super().execute_activity(input)


class TemporalWorkerTelemetryInterceptor(WorkerInterceptor):
    def intercept_activity(self, next: ActivityInboundInterceptor) -> ActivityInboundInterceptor:
        return TemporalActivityTelemetryInterceptor(next)
