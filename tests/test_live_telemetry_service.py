from __future__ import annotations

import json
from datetime import datetime, timezone

from orchestrator.api.admin.live_telemetry_service import list_live_workflow_telemetry_events
from orchestrator.core.config import Settings


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


def test_list_live_workflow_telemetry_events_maps_loki_rows(monkeypatch) -> None:
    captured: dict[str, object] = {}
    timestamp_ns = str(int(datetime(2026, 4, 20, 17, 40, 0, tzinfo=timezone.utc).timestamp() * 1_000_000_000))
    payload = {
        "data": {
            "result": [
                {
                    "stream": {
                        "service_name": "api",
                        "service_namespace": "master-builder",
                        "scope_name": "orchestrator.workflow_operation",
                        "tenant_id": "example",
                        "detected_level": "error",
                        "event_type": "workflow_operation_attempt_failed",
                        "metadata_workflow_id": "parent_planning:MAB-215",
                        "metadata_operation_id": "operation-jira-child-fanout",
                        "metadata_attempt_id": "attempt-3",
                        "metadata_attempt_number": "3",
                        "metadata_run_id": "",
                        "metadata_status": "failed",
                        "metadata_error_category": "content_limit",
                        "metadata_response_payload_json": '{"children":[{"key":"MAB-301","summary":"Create tenant assurance boundary"}]}',
                    },
                    "values": [
                        [
                            timestamp_ns,
                            'Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
                        ]
                    ],
                }
            ]
        }
    }

    def _fake_urlopen(request, timeout):  # noqa: ANN001
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _FakeResponse(payload)

    monkeypatch.setattr("orchestrator.api.admin.live_telemetry_service.urllib_request.urlopen", _fake_urlopen)

    events = list_live_workflow_telemetry_events(
        settings=Settings(
            observability_loki_query_base_url="http://loki:3100",
            otel_service_namespace="master-builder",
        ),
        tenant_id="example",
        workflow_id="parent_planning:MAB-215",
        operation_id="operation-jira-child-fanout",
        limit=50,
    )

    assert len(events) == 1
    event = events[0]
    assert event.source == "telemetry"
    assert event.level == "error"
    assert event.event_kind == "workflow_operation_attempt_failed"
    assert event.message.startswith("Jira API request failed")
    assert event.operation_id == "operation-jira-child-fanout"
    assert event.attempt_id == "attempt-3"
    assert event.attempt == 3
    assert event.source_component == "orchestrator.workflow_operation"
    assert event.payload["status"] == "failed"
    assert event.payload["error_category"] == "content_limit"
    assert event.payload["response_payload"]["children"][0]["key"] == "MAB-301"
    assert 'service_name%3D~%22api%7Crun-worker%7Cwebhook-worker%7Ctemporal-worker%7Cproject-automation%7Cknowledge-sync%22' in str(captured["url"])
    assert "metadata_workflow_id=parent_planning%3AMAB-215" not in str(captured["url"])
    assert 'metadata_workflow_id%3D%22parent_planning%3AMAB-215%22' not in str(captured["url"])
    assert 'metadata_operation_id%3D%22operation-jira-child-fanout%22' not in str(captured["url"])
