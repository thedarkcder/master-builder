from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from orchestrator.core.observability.agent_observability import record_agent_lifecycle_event
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.observability.logging_pane import emit_logging_pane_event
from orchestrator.core.workflow.execution_lifecycle import apply_execution_failure
from orchestrator.core.runs.service import RUN_STATUS_DISPATCHING, RUN_STATUS_FAILED, RUN_STATUS_RUNNING
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Run, WorkflowExecution

logger = logging.getLogger(__name__)


def worker_service_instance_id() -> str:
    settings = get_settings()
    agent_id = str(getattr(settings, "agent_id", "") or "").strip()
    if agent_id:
        return agent_id
    return os.uname().nodename


def worker_service_instance_id_for_mode(*, settings: Settings | None = None, mode: str | None = None) -> str:
    resolved_settings = settings or get_settings()
    base_id = str(getattr(resolved_settings, "agent_id", "") or "").strip() or os.uname().nodename
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode:
        return f"{base_id}:{normalized_mode}"
    return base_id


def stale_run_cutoff(*, settings: Settings, now: datetime | None = None) -> datetime:
    heartbeat_timeout_seconds = max(60, int(getattr(settings, "worker_run_stale_timeout_seconds", 300)))
    anchor = now or datetime.now(timezone.utc)
    return anchor - timedelta(seconds=heartbeat_timeout_seconds)


def _effective_last_seen_expr():  # noqa: ANN202
    return func.coalesce(Run.last_heartbeat_at, Run.started_at, Run.dispatch_claimed_at, Run.created_at)


def touch_run_heartbeat(
    session: Session,
    *,
    run_id: str,
    worker_service_instance_id: str,
    claim_id: str,
    heartbeat_at: datetime | None = None,
) -> bool:
    timestamp = heartbeat_at or datetime.now(timezone.utc)
    normalized_owner = str(worker_service_instance_id or "").strip()
    normalized_claim_id = str(claim_id or "").strip()
    if not normalized_owner or not normalized_claim_id:
        return False
    result = session.execute(
        update(Run)
        .where(
            Run.run_id == run_id,
            Run.status == RUN_STATUS_RUNNING,
            Run.worker_service_instance_id == normalized_owner,
            Run.claim_id == normalized_claim_id,
        )
        .values(last_heartbeat_at=timestamp)
    )
    if int(result.rowcount or 0) == 0:
        session.rollback()
        return False
    session.commit()
    return True


@dataclass(frozen=True)
class StaleRunRecoveryRecord:
    run_id: str
    tenant_id: str
    issue_key: str
    project_id: str | None
    previous_owner: str | None
    last_heartbeat_at: datetime | None


def recover_stale_running_runs(
    *,
    session: Session,
    settings: Settings,
    recovered_by_agent_id: str,
    recovered_by_service_instance_id: str,
    now: datetime | None = None,
) -> list[StaleRunRecoveryRecord]:
    cutoff = stale_run_cutoff(settings=settings, now=now)
    rows = session.execute(
        select(Run)
        .where(
            Run.status.in_((RUN_STATUS_DISPATCHING, RUN_STATUS_RUNNING)),
            _effective_last_seen_expr() <= cutoff,
        )
        .order_by(Run.started_at.asc(), Run.created_at.asc())
    ).scalars().all()

    recovered: list[StaleRunRecoveryRecord] = []
    for row in rows:
        last_seen = row.last_heartbeat_at or row.started_at or row.dispatch_claimed_at or row.created_at
        recovered_at = now or datetime.now(timezone.utc)
        stale_status = str(row.status or "").strip().lower()
        stale_kind = "dispatching" if stale_status == RUN_STATUS_DISPATCHING else "running"
        message = (
            f"Recovered stale {stale_kind} run after heartbeat timeout. "
            f"previous_owner={row.worker_service_instance_id or 'unknown'} "
            f"last_heartbeat_at={(last_seen.isoformat() if last_seen is not None else 'unknown')} "
            f"recovered_by={recovered_by_service_instance_id}"
        )
        result = session.execute(
            update(Run)
            .where(
                Run.run_id == row.run_id,
                Run.status.in_((RUN_STATUS_DISPATCHING, RUN_STATUS_RUNNING)),
                _effective_last_seen_expr() <= cutoff,
            )
            .values(
                status=RUN_STATUS_FAILED,
                last_error=message,
                claim_id=None,
                dispatch_claimed_at=None,
                started_at=func.coalesce(Run.started_at, recovered_at),
                finished_at=recovered_at,
                worker_service_instance_id=None,
            )
        )
        if int(result.rowcount or 0) == 0:
            session.rollback()
            continue
        workflow = session.get(WorkflowExecution, row.workflow_id)
        if workflow is not None and workflow.status in {"queued", RUN_STATUS_RUNNING}:
            apply_execution_failure(
                workflow=workflow,
                message=message,
                now=recovered_at,
            )
        emit_logging_pane_event(
            session=session,
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            run_id=row.run_id,
            issue_key=row.issue_key,
            agent_id=recovered_by_agent_id,
            invocation_id=uuid4().hex,
            channel="worker",
            command="workflow.stale_recovery",
            working_dir=None,
            stage="telemetry",
            attempt=None,
            stream="system",
            message=json.dumps(
                {
                    "event_kind": "stale_run_recovered",
                    "previous_owner": row.worker_service_instance_id,
                    "last_heartbeat_at": last_seen.isoformat() if last_seen is not None else None,
                    "recovered_by": recovered_by_service_instance_id,
                    "stale_timeout_seconds": max(60, int(getattr(settings, "worker_run_stale_timeout_seconds", 300))),
                },
                sort_keys=True,
            ),
        )
        record_agent_lifecycle_event(
            session=session,
            event_type="RUN_FAILED",
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            run_id=row.run_id,
            issue_key=row.issue_key,
            agent_id=recovered_by_agent_id,
        )
        record_agent_lifecycle_event(
            session=session,
            event_type="TASK_FAILED",
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            run_id=row.run_id,
            issue_key=row.issue_key,
            agent_id=recovered_by_agent_id,
        )
        session.commit()
        run = session.get(Run, row.run_id)
        if run is None:
            continue
        recovered.append(
            StaleRunRecoveryRecord(
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                project_id=run.project_id,
                previous_owner=row.worker_service_instance_id,
                last_heartbeat_at=last_seen,
            )
        )
    return recovered


@dataclass
class WorkerRunHeartbeatController:
    database_url: str
    run_id: str
    worker_service_instance_id: str
    claim_id: str
    heartbeat_interval_seconds: int

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
            name=f"run-heartbeat-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, float(self.heartbeat_interval_seconds)))

    def _run(self) -> None:
        interval_seconds = max(5, int(self.heartbeat_interval_seconds))
        while not self._stop_event.wait(interval_seconds):
            try:
                with self._session_factory() as session:
                    updated = touch_run_heartbeat(
                        session,
                        run_id=self.run_id,
                        worker_service_instance_id=self.worker_service_instance_id,
                        claim_id=self.claim_id,
                    )
                if not updated:
                    return
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "worker_run_heartbeat_failed run_id=%s service_instance_id=%s error=%s",
                    self.run_id,
                    self.worker_service_instance_id,
                    exc,
                )
                continue
