from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace

from orchestrator.temporal.client import connect_temporal_client
from orchestrator.temporal.telemetry import (
    TemporalActivityTelemetryInterceptor,
    TemporalClientTelemetryOutboundInterceptor,
)
from orchestrator.temporal.worker import run_temporal_worker


def test_connect_temporal_client_registers_telemetry_interceptor(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeClient:
        @staticmethod
        async def connect(target_host, **kwargs):  # noqa: ANN001
            captured["target_host"] = target_host
            captured["kwargs"] = kwargs
            return "client"

    monkeypatch.setattr("temporalio.client.Client", _FakeClient)

    result = asyncio.run(
        connect_temporal_client(
            SimpleNamespace(
                temporal_target_host="127.0.0.1:7233",
                temporal_namespace="default",
            )
        )
    )

    assert result == "client"
    assert captured["kwargs"]["namespace"] == "default"
    assert len(captured["kwargs"]["interceptors"]) == 1


def test_temporal_client_outbound_interceptor_traces_start_workflow(monkeypatch) -> None:
    captured: dict[str, object] = {}

    @contextmanager
    def _fake_span(name: str, *, attributes=None):  # noqa: ANN001
        captured["name"] = name
        captured["attributes"] = attributes
        yield None

    class _Next:
        async def start_workflow(self, input):  # noqa: ANN001
            captured["input"] = input
            return "ok"

    monkeypatch.setattr("orchestrator.temporal.telemetry.telemetry_span", _fake_span)

    interceptor = TemporalClientTelemetryOutboundInterceptor(_Next())
    result = asyncio.run(
        interceptor.start_workflow(
            SimpleNamespace(
                id="workflow:parent_planning:MAB-215",
                workflow="handler_backed_workflow",
                task_queue="master-builder",
            )
        )
    )

    assert result == "ok"
    assert captured["name"] == "temporal.client.start_workflow"
    assert captured["attributes"]["workflow.id"] == "parent_planning:MAB-215"
    assert captured["attributes"]["temporal.handle_id"] == "workflow:parent_planning:MAB-215"
    assert captured["attributes"]["temporal.task_queue"] == "master-builder"


def test_temporal_activity_interceptor_traces_execute_activity(monkeypatch) -> None:
    captured: dict[str, object] = {}

    @contextmanager
    def _fake_span(name: str, *, attributes=None):  # noqa: ANN001
        captured["name"] = name
        captured["attributes"] = attributes
        yield None

    class _Next:
        def init(self, outbound):  # noqa: ANN001
            return None

        async def execute_activity(self, input):  # noqa: ANN001
            captured["input"] = input
            return "activity-ok"

    monkeypatch.setattr("orchestrator.temporal.telemetry.telemetry_span", _fake_span)
    monkeypatch.setattr(
        "orchestrator.temporal.telemetry.activity.info",
        lambda: SimpleNamespace(
            activity_id="activity-1",
            activity_type="process_handler_workflow_advance_activity",
            attempt=2,
            workflow_id="workflow:parent_planning:MAB-215",
            workflow_run_id="run-123",
            workflow_type="HandlerBackedWorkflow",
            task_queue="master-builder",
        ),
    )

    interceptor = TemporalActivityTelemetryInterceptor(_Next())
    result = asyncio.run(
        interceptor.execute_activity(
            SimpleNamespace(
                fn=SimpleNamespace(__name__="process_handler_workflow_advance_activity"),
                args=(),
                headers={},
            )
        )
    )

    assert result == "activity-ok"
    assert captured["name"] == "temporal.activity.execute"
    assert captured["attributes"]["workflow.id"] == "parent_planning:MAB-215"
    assert captured["attributes"]["temporal.activity_attempt"] == 2
    assert captured["attributes"]["temporal.activity_type"] == "process_handler_workflow_advance_activity"


def test_run_temporal_worker_registers_activity_interceptor(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeWorker:
        def __init__(self, client, **kwargs):  # noqa: ANN001
            captured["client"] = client
            captured["kwargs"] = kwargs

        async def run(self):
            captured["ran"] = True

    monkeypatch.setattr("temporalio.worker.Worker", _FakeWorker)
    monkeypatch.setattr("orchestrator.temporal.worker.connect_temporal_client", lambda _settings: asyncio.sleep(0, result="client"))
    monkeypatch.setattr(
        "orchestrator.temporal.worker.get_settings",
        lambda: SimpleNamespace(
            log_level="INFO",
            sentry_environment="test",
            sentry_release="test-release",
        ),
    )
    monkeypatch.setattr("orchestrator.temporal.worker.configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr("orchestrator.temporal.worker.initialize_telemetry", lambda **kwargs: True)

    asyncio.run(run_temporal_worker())

    assert captured["client"] == "client"
    assert len(captured["kwargs"]["interceptors"]) == 1
    assert captured["ran"] is True
