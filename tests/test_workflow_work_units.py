from __future__ import annotations

from datetime import datetime, timezone
import pytest
from unittest.mock import patch

from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
    PARENT_WU_BACKLOG_SECURITY_MODEL,
)
from orchestrator.core.workflow.work_units import (
    WorkflowWorkUnitContractError,
    WorkflowWorkUnitRetryExhaustedError,
    run_work_unit,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import (
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
    WorkflowOperationWorkUnit,
    WorkflowOperationWorkUnitAttempt,
)
from tests.test_support.db_harness import SqliteTemplateDbTestCase


def _workflow(now: datetime) -> WorkflowExecution:
    return WorkflowExecution(
        workflow_id="parent_planning:MAB-900",
        execution_id="wfexec-mab-900",
        workflow_type_key="parent_planning",
        tenant_id="tenant-a",
        project_id="tenant-a-default",
        source_system="jira",
        source_ref="MAB-900",
        display_name="Durable work units",
        source_description="Parent planning",
        repo_url=None,
        branch=None,
        pr_url=None,
        orchestration_backend="temporal",
        dedupe_scope="parent_planning",
        status="running",
        last_error=None,
        active_run_id=None,
        latest_checkpoint_id=None,
        source_workflow_id=None,
        source_run_id=None,
        created_at=now,
        started_at=now,
        finished_at=None,
        updated_at=now,
    )


def _operation(now: datetime) -> WorkflowOperation:
    return WorkflowOperation(
        operation_id="operation-backlog-planning",
        workflow_id="parent_planning:MAB-900",
        run_id=None,
        operation_type=PARENT_OP_BACKLOG_PLANNING,
        idempotency_key="backlog-planning:MAB-900",
        status="running",
        target_system=None,
        target_ref=None,
        summary=None,
        created_at=now,
        started_at=now,
        finished_at=None,
        updated_at=now,
    )


def _attempt(
    now: datetime, *, attempt_id: str, attempt_number: int, status: str = "running"
) -> WorkflowOperationAttempt:
    return WorkflowOperationAttempt(
        attempt_id=attempt_id,
        operation_id="operation-backlog-planning",
        attempt_number=attempt_number,
        status=status,
        error_category=None,
        error_message=None,
        status_detail=None,
        retryable=False,
        next_retry_at=None,
        last_heartbeat_at=now if status == "running" else None,
        lease_expires_at=None,
        lease_owner=None,
        created_at=now,
        started_at=now,
        finished_at=None if status == "running" else now,
    )


class WorkflowWorkUnitTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(
            name_prefix="workflow-work-units"
        )

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_completed_unit_output_is_reused_on_new_operation_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = _workflow(now)
            operation = _operation(now)
            attempt_1 = _attempt(now, attempt_id="attempt-1", attempt_number=1)
            session.add_all([workflow, operation, attempt_1])
            session.commit()

            calls = {"architecture": 0}

            result_1 = run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt_1,
                unit_key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                idempotency_key="MAB-900:architecture",
                input_payload={"brief": "same"},
                execute=lambda _context: (
                    calls.__setitem__("architecture", calls["architecture"] + 1)
                    or {"ok": True}
                ),
                serialize=lambda result: {"result": result},
                deserialize=lambda payload: dict(payload["result"]),
            )
            attempt_1.status = "failed"
            attempt_1.finished_at = now
            attempt_2 = _attempt(now, attempt_id="attempt-2", attempt_number=2)
            session.add(attempt_2)
            session.commit()

            result_2 = run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt_2,
                unit_key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                idempotency_key="MAB-900:architecture",
                input_payload={"brief": "same"},
                execute=lambda _context: (
                    calls.__setitem__("architecture", calls["architecture"] + 1)
                    or {"ok": False}
                ),
                serialize=lambda result: {"result": result},
                deserialize=lambda payload: dict(payload["result"]),
            )

            assert result_1 == {"ok": True}
            assert result_2 == {"ok": True}
            assert calls["architecture"] == 1
            unit = session.query(WorkflowOperationWorkUnit).one()
            attempts = (
                session.query(WorkflowOperationWorkUnitAttempt)
                .filter_by(work_unit_id=unit.work_unit_id)
                .order_by(WorkflowOperationWorkUnitAttempt.attempt_number.asc())
                .all()
            )
            assert [item.operation_attempt_id for item in attempts] == [
                "attempt-1",
                "attempt-2",
            ]
            assert [item.status for item in attempts] == ["completed", "completed"]

    def test_same_idempotency_key_with_different_input_fails_hard(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = _workflow(now)
            operation = _operation(now)
            attempt = _attempt(now, attempt_id="attempt-1", attempt_number=1)
            session.add_all([workflow, operation, attempt])
            session.commit()

            run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt,
                unit_key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                idempotency_key="MAB-900:architecture",
                input_payload={"brief": "first"},
                execute=lambda _context: {"ok": True},
                serialize=lambda result: {"result": result},
                deserialize=lambda payload: dict(payload["result"]),
            )

            with pytest.raises(WorkflowWorkUnitContractError, match="different input"):
                run_work_unit(
                    session,
                    operation=operation,
                    operation_attempt=attempt,
                    unit_key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                    idempotency_key="MAB-900:architecture",
                    input_payload={"brief": "changed"},
                    execute=lambda _context: {"ok": False},
                    serialize=lambda result: {"result": result},
                    deserialize=lambda payload: dict(payload["result"]),
                )

    def test_failed_unit_retries_without_rerunning_completed_peer_unit(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = _workflow(now)
            operation = _operation(now)
            attempt_1 = _attempt(now, attempt_id="attempt-1", attempt_number=1)
            session.add_all([workflow, operation, attempt_1])
            session.commit()

            calls = {"architecture": 0, "security": 0}
            run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt_1,
                unit_key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                idempotency_key="MAB-900:architecture",
                input_payload={"brief": "same"},
                execute=lambda _context: (
                    calls.__setitem__("architecture", calls["architecture"] + 1)
                    or {"architecture": "done"}
                ),
                serialize=lambda result: {"result": result},
                deserialize=lambda payload: dict(payload["result"]),
            )
            with pytest.raises(RuntimeError, match="bad security payload"):
                run_work_unit(
                    session,
                    operation=operation,
                    operation_attempt=attempt_1,
                    unit_key=PARENT_WU_BACKLOG_SECURITY_MODEL,
                    idempotency_key="MAB-900:security",
                    input_payload={"brief": "same"},
                    execute=lambda _context: (
                        calls.__setitem__("security", calls["security"] + 1)
                        or (_ for _ in ()).throw(RuntimeError("bad security payload"))
                    ),
                    serialize=lambda result: {"result": result},
                    deserialize=lambda payload: dict(payload["result"]),
                )
            attempt_1.status = "failed"
            attempt_1.finished_at = now
            attempt_2 = _attempt(now, attempt_id="attempt-2", attempt_number=2)
            session.add(attempt_2)
            session.commit()

            reused = run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt_2,
                unit_key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                idempotency_key="MAB-900:architecture",
                input_payload={"brief": "same"},
                execute=lambda _context: (
                    calls.__setitem__("architecture", calls["architecture"] + 1)
                    or {"architecture": "rerun"}
                ),
                serialize=lambda result: {"result": result},
                deserialize=lambda payload: dict(payload["result"]),
            )
            recovered = run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt_2,
                unit_key=PARENT_WU_BACKLOG_SECURITY_MODEL,
                idempotency_key="MAB-900:security",
                input_payload={"brief": "same"},
                execute=lambda _context: (
                    calls.__setitem__("security", calls["security"] + 1)
                    or {"security": "done"}
                ),
                serialize=lambda result: {"result": result},
                deserialize=lambda payload: dict(payload["result"]),
            )

            assert reused == {"architecture": "done"}
            assert recovered == {"security": "done"}
            assert calls == {"architecture": 1, "security": 2}

    def test_exhausted_failed_unit_can_run_on_new_operation_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = _workflow(now)
            operation = _operation(now)
            attempt_1 = _attempt(now, attempt_id="attempt-1", attempt_number=1)
            session.add_all([workflow, operation, attempt_1])
            session.commit()

            calls = {"security": 0}
            for _ in range(3):
                with pytest.raises(RuntimeError, match="temporary projection failure"):
                    run_work_unit(
                        session,
                        operation=operation,
                        operation_attempt=attempt_1,
                        unit_key=PARENT_WU_BACKLOG_SECURITY_MODEL,
                        idempotency_key="MAB-900:security",
                        input_payload={"brief": "same"},
                        execute=lambda _context: (
                            calls.__setitem__("security", calls["security"] + 1)
                            or (_ for _ in ()).throw(
                                RuntimeError("temporary projection failure")
                            )
                        ),
                        serialize=lambda result: {"result": result},
                        deserialize=lambda payload: dict(payload["result"]),
                    )
                session.commit()

            with pytest.raises(
                WorkflowWorkUnitRetryExhaustedError, match="exhausted 3 attempts"
            ):
                run_work_unit(
                    session,
                    operation=operation,
                    operation_attempt=attempt_1,
                    unit_key=PARENT_WU_BACKLOG_SECURITY_MODEL,
                    idempotency_key="MAB-900:security",
                    input_payload={"brief": "same"},
                    execute=lambda _context: {"security": "should-not-run"},
                    serialize=lambda result: {"result": result},
                    deserialize=lambda payload: dict(payload["result"]),
                )

            attempt_1.status = "failed"
            attempt_1.finished_at = now
            attempt_2 = _attempt(now, attempt_id="attempt-2", attempt_number=2)
            session.add(attempt_2)
            session.commit()

            recovered = run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt_2,
                unit_key=PARENT_WU_BACKLOG_SECURITY_MODEL,
                idempotency_key="MAB-900:security",
                input_payload={"brief": "same"},
                execute=lambda _context: {"security": "done"},
                serialize=lambda result: {"result": result},
                deserialize=lambda payload: dict(payload["result"]),
            )

            assert recovered == {"security": "done"}
            assert calls["security"] == 3

    def test_run_work_unit_starts_and_stops_operation_heartbeat_controller(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = _workflow(now)
            operation = _operation(now)
            attempt = _attempt(now, attempt_id="attempt-1", attempt_number=1)
            session.add_all([workflow, operation, attempt])
            session.commit()

            controller_events: list[tuple[str, str | None, str | None]] = []

            class _Controller:
                def __init__(
                    self, *, database_url: str, attempt_id: str, lease_owner: str
                ) -> None:
                    controller_events.append(("init", attempt_id, lease_owner))

                def start(self) -> None:
                    controller_events.append(("start", None, None))

                def stop(self) -> None:
                    controller_events.append(("stop", None, None))

            with patch(
                "orchestrator.core.workflow.work_units.WorkflowOperationAttemptHeartbeatController",
                _Controller,
            ):
                result = run_work_unit(
                    session,
                    operation=operation,
                    operation_attempt=attempt,
                    unit_key=PARENT_WU_BACKLOG_ARCHITECTURE_MODEL,
                    idempotency_key="MAB-900:architecture",
                    input_payload={"brief": "same"},
                    execute=lambda _context: (
                        controller_events.append(("execute", None, None))
                        or {"ok": True}
                    ),
                    serialize=lambda payload: {"result": payload},
                    deserialize=lambda payload: dict(payload["result"]),
                )

            assert result == {"ok": True}
            assert controller_events == [
                ("init", "attempt-1", "workflow_work_units"),
                ("start", None, None),
                ("execute", None, None),
                ("stop", None, None),
            ]
