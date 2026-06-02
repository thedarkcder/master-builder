from __future__ import annotations

from dataclasses import dataclass
import logging
import threading

from orchestrator.core.config import get_settings
from orchestrator.core.workflow.operation_service import ACTIVE_OPERATION_ATTEMPT_STATUSES, touch_workflow_operation_attempt_heartbeat
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowOperationAttempt

logger = logging.getLogger(__name__)


@dataclass
class WorkflowOperationAttemptHeartbeatController:
    database_url: str
    attempt_id: str
    lease_owner: str

    def __post_init__(self) -> None:
        self._session_factory = create_session_factory(self.database_url)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"workflow-operation-heartbeat-{self.attempt_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, float(self._interval_seconds())))

    def _interval_seconds(self) -> int:
        raw_interval = int(getattr(get_settings(), "workflow_operation_attempt_heartbeat_interval_seconds", 30))
        return max(1, raw_interval)

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds()):
            try:
                with self._session_factory() as session:
                    attempt = session.get(WorkflowOperationAttempt, self.attempt_id)
                    if attempt is None:
                        return
                    if str(attempt.status or "").strip().lower() not in ACTIVE_OPERATION_ATTEMPT_STATUSES:
                        return
                    touch_workflow_operation_attempt_heartbeat(
                        session,
                        attempt=attempt,
                        lease_owner=self.lease_owner,
                    )
                    session.commit()
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "workflow_operation_attempt_heartbeat_failed attempt_id=%s lease_owner=%s error=%s",
                    self.attempt_id,
                    self.lease_owner,
                    exc,
                )
                continue
