from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from orchestrator.api.admin import run_logging_stream_service, runtime_logs_service, workflow_live_stream_service


def _row(sequence: int):
    return SimpleNamespace(event_sequence=sequence, payload_json={})


def test_workflow_stream_uses_shared_notification_marker_after_snapshot(monkeypatch) -> None:
    wait_calls: list[tuple[int, float]] = []
    after_calls = 0

    monkeypatch.setattr(
        workflow_live_stream_service,
        "build_observability_snapshot_query",
        lambda **_kwargs: [_row(10)],
    )
    monkeypatch.setattr(workflow_live_stream_service, "encode_stream_row", lambda row: f"{row.event_sequence}\n")
    monkeypatch.setattr(workflow_live_stream_service, "current_product_event_notification_marker", lambda: 3)

    def _wait(*, marker: int, timeout_seconds: float) -> int:
        wait_calls.append((marker, timeout_seconds))
        return marker + 1

    def _list_after(**_kwargs):
        nonlocal after_calls
        after_calls += 1
        return [] if after_calls == 1 else [_row(11)]

    monkeypatch.setattr(workflow_live_stream_service, "wait_for_product_event_notification", _wait)
    monkeypatch.setattr(workflow_live_stream_service, "list_product_events_after_sequence", _list_after)

    stream = workflow_live_stream_service.stream_workflow_operation_live_events_ndjson(
        operation_id="operation-1",
        attempt_id="attempt-1",
        settings=SimpleNamespace(logging_pane_initial_log_limit=200),
    )

    assert next(stream) == "10\n"
    assert next(stream) == "11\n"
    assert wait_calls == [(3, 25)]


def test_run_stream_waits_on_shared_notifications_instead_of_polling(monkeypatch) -> None:
    monkeypatch.setattr(run_logging_stream_service, "build_run_logging_stream_snapshot_query", lambda **_kwargs: [])
    monkeypatch.setattr(run_logging_stream_service, "current_product_event_notification_marker", lambda: 7)
    monkeypatch.setattr(run_logging_stream_service, "encode_logging_pane_stream_row", lambda row: f"{row.event_sequence}\n")
    monkeypatch.setattr(run_logging_stream_service, "list_logging_pane_events_after_sequence", lambda **_kwargs: [])

    wait_calls: list[tuple[int, float]] = []

    def _wait(*, marker: int, timeout_seconds: float) -> int:
        wait_calls.append((marker, timeout_seconds))
        return marker

    monkeypatch.setattr(run_logging_stream_service, "wait_for_product_event_notification", _wait)

    stream = run_logging_stream_service.stream_run_events_ndjson(
        session=SimpleNamespace(get=lambda _model, _run_id: object()),
        run_id="run-1",
        run_model=object,
        settings=SimpleNamespace(run_events_initial_limit=100, logging_pane_initial_log_limit=200),
    )

    assert next(stream) == "\n"
    assert wait_calls == [(7, 25)]


def test_runtime_stream_waits_on_shared_notifications_instead_of_polling(monkeypatch) -> None:
    monkeypatch.setattr(runtime_logs_service, "build_runtime_logging_stream_snapshot_query", lambda **_kwargs: [])
    monkeypatch.setattr(runtime_logs_service, "current_product_event_notification_marker", lambda: 9)
    monkeypatch.setattr(runtime_logs_service, "encode_logging_pane_stream_row", lambda row: f"{row.event_sequence}\n")
    monkeypatch.setattr(runtime_logs_service, "list_logging_pane_events_after_sequence", lambda **_kwargs: [])

    wait_calls: list[tuple[int, float]] = []

    def _wait(*, marker: int, timeout_seconds: float) -> int:
        wait_calls.append((marker, timeout_seconds))
        return marker

    monkeypatch.setattr(runtime_logs_service, "wait_for_product_event_notification", _wait)

    stream = runtime_logs_service.stream_runtime_events_ndjson(
        session=object(),
        settings=SimpleNamespace(event_stream_poll_ms=1),
        tenant_id="tenant-a",
    )

    assert next(stream) == "\n"
    assert wait_calls == [(9, 25)]


def test_stream_services_do_not_open_per_client_postgres_listeners() -> None:
    for path in (
        Path("orchestrator/api/admin/workflow_live_stream_service.py"),
        Path("orchestrator/api/admin/run_logging_stream_service.py"),
        Path("orchestrator/api/admin/runtime_logs_service.py"),
    ):
        source = path.read_text()
        assert "open_product_event_listener" not in source
