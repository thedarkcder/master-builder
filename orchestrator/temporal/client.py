from __future__ import annotations


def temporal_task_queue(settings) -> str:  # noqa: ANN001
    return str(getattr(settings, "temporal_task_queue", "") or "").strip() or "master-builder"


async def connect_temporal_client(settings):  # noqa: ANN001, ANN201
    from temporalio.client import Client
    from orchestrator.temporal.telemetry import TemporalClientTelemetryInterceptor

    target_host = str(getattr(settings, "temporal_target_host", "") or "").strip() or "127.0.0.1:7233"
    namespace = str(getattr(settings, "temporal_namespace", "") or "").strip() or "default"
    return await Client.connect(
        target_host,
        namespace=namespace,
        interceptors=[TemporalClientTelemetryInterceptor()],
    )
