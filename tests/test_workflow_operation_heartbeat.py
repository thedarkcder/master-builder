from __future__ import annotations

from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.workflow.operation_heartbeat import WorkflowOperationAttemptHeartbeatController


class _FakeSession:
    def __init__(self, attempt) -> None:  # noqa: ANN001
        self._attempt = attempt

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False

    def get(self, _model, _attempt_id):  # noqa: ANN001
        return self._attempt

    def commit(self) -> None:
        return None


def test_operation_attempt_heartbeat_controller_touches_running_attempt() -> None:
    touched = Event()
    attempt = SimpleNamespace(status="running")

    def _session_factory():  # noqa: ANN202
        return _FakeSession(attempt)

    def _touch(_session, *, lease_owner, **_kwargs):  # noqa: ANN001, ANN202
        assert lease_owner == "workflow_work_units"
        touched.set()

    with (
        patch("orchestrator.core.workflow.operation_heartbeat.create_session_factory", return_value=_session_factory),
        patch(
            "orchestrator.core.workflow.operation_heartbeat.get_settings",
            return_value=SimpleNamespace(workflow_operation_attempt_heartbeat_interval_seconds=1),
        ),
        patch("orchestrator.core.workflow.operation_heartbeat.touch_workflow_operation_attempt_heartbeat", side_effect=_touch),
    ):
        controller = WorkflowOperationAttemptHeartbeatController(
            database_url="sqlite:////tmp/test.db",
            attempt_id="attempt-1",
            lease_owner="workflow_work_units",
        )
        controller.start()
        try:
            assert touched.wait(2.5), "expected heartbeat controller to refresh the operation attempt lease"
        finally:
            controller.stop()
