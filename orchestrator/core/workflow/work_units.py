from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any, TypeVar
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.definition import (
    WorkflowDefinition,
    WorkflowWorkUnitDefinition,
    WorkflowWorkUnitKind,
)
from orchestrator.core.workflow.operation_heartbeat import WorkflowOperationAttemptHeartbeatController
from orchestrator.core.workflow.operation_logging import emit_workflow_operation_log
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.models import (
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
    WorkflowOperationWorkUnit,
    WorkflowOperationWorkUnitAttempt,
)

T = TypeVar("T")

WORK_UNIT_STATUS_RUNNING = "running"
WORK_UNIT_STATUS_FAILED = "failed"
WORK_UNIT_STATUS_COMPLETED = "completed"
WORK_UNIT_STATUS_RETRYING = "retrying"


class WorkflowWorkUnitContractError(RuntimeError):
    """Raised when a work unit violates the workflow durability contract."""


class WorkflowWorkUnitRetryExhaustedError(RuntimeError):
    """Raised when a work unit fails and has no remaining unit-level attempts."""


@dataclass(frozen=True)
class WorkflowWorkUnitAttemptRef:
    work_unit_id: str
    work_unit_attempt_id: str
    unit_key: str
    unit_kind: str
    attempt_number: int


@dataclass(frozen=True)
class WorkflowWorkUnitContext:
    workflow: WorkflowExecution
    workflow_type: WorkflowDefinition
    operation: WorkflowOperation
    operation_attempt: WorkflowOperationAttempt
    definition: WorkflowWorkUnitDefinition
    work_unit: WorkflowOperationWorkUnit
    attempt: WorkflowOperationWorkUnitAttempt

    @property
    def ref(self) -> WorkflowWorkUnitAttemptRef:
        return WorkflowWorkUnitAttemptRef(
            work_unit_id=self.work_unit.work_unit_id,
            work_unit_attempt_id=self.attempt.work_unit_attempt_id,
            unit_key=self.definition.key,
            unit_kind=self.definition.kind.value,
            attempt_number=self.attempt.attempt_number,
        )


@dataclass(frozen=True)
class WorkflowWorkUnitResult:
    output: Any
    summary: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _input_fingerprint(value: object) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def workflow_work_unit_input_fingerprint(value: object) -> str:
    return _input_fingerprint(value)


def _default_serialize(value: T) -> dict[str, object]:
    if isinstance(value, WorkflowWorkUnitResult):
        return {"output": value.output, "summary": value.summary}
    return {"output": value}


def _default_deserialize(payload: dict[str, object]) -> Any:
    return payload.get("output")


def _attempt_ref(operation: WorkflowOperation, attempt: WorkflowOperationAttempt):
    from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef

    return WorkflowAttemptRef(
        workflow_id=operation.workflow_id,
        operation_id=operation.operation_id,
        attempt_id=attempt.attempt_id,
        number=attempt.attempt_number,
    )


def _resolve_definition(
    *,
    session: Session,
    operation: WorkflowOperation,
    unit_key: str,
) -> tuple[WorkflowExecution, WorkflowDefinition, WorkflowWorkUnitDefinition]:
    workflow = session.get(WorkflowExecution, operation.workflow_id)
    if workflow is None:
        raise WorkflowWorkUnitContractError(f"Workflow {operation.workflow_id} is missing for work unit {unit_key}.")
    workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
    definition = workflow_type.work_unit(unit_key)
    if definition.step_key != operation.operation_type:
        raise WorkflowWorkUnitContractError(
            f"Workflow work unit {unit_key} belongs to step {definition.step_key}, "
            f"not operation {operation.operation_type}."
        )
    return workflow, workflow_type, definition


def _validate_idempotency(
    *,
    definition: WorkflowWorkUnitDefinition,
    idempotency_key: str,
) -> str:
    normalized = str(idempotency_key or "").strip()
    if not normalized:
        raise WorkflowWorkUnitContractError(f"Workflow work unit {definition.key} requires idempotency_key.")
    if definition.kind in {WorkflowWorkUnitKind.EXTERNAL_API, WorkflowWorkUnitKind.SIDE_EFFECT}:
        if not definition.idempotency_policy.required:
            raise WorkflowWorkUnitContractError(
                f"Workflow work unit {definition.key} performs side effects without required idempotency."
            )
    return normalized


def _backoff_seconds(*, definition: WorkflowWorkUnitDefinition, attempt_number: int) -> int:
    policy = definition.retry_policy
    if attempt_number >= policy.max_attempts:
        return 0
    if policy.initial_interval_seconds <= 0:
        return 0
    calculated = policy.initial_interval_seconds * (policy.backoff_coefficient ** max(0, attempt_number - 1))
    if policy.max_interval_seconds > 0:
        calculated = min(calculated, policy.max_interval_seconds)
    return int(calculated)


def _get_or_create_work_unit(
    *,
    session: Session,
    operation: WorkflowOperation,
    parent_attempt: WorkflowOperationAttempt,
    definition: WorkflowWorkUnitDefinition,
    idempotency_key: str,
    input_fingerprint: str,
) -> WorkflowOperationWorkUnit:
    mismatched = session.execute(
        select(WorkflowOperationWorkUnit).where(
            WorkflowOperationWorkUnit.operation_id == operation.operation_id,
            WorkflowOperationWorkUnit.unit_key == definition.key,
            WorkflowOperationWorkUnit.idempotency_key == idempotency_key,
            WorkflowOperationWorkUnit.input_fingerprint != input_fingerprint,
        )
    ).scalar_one_or_none()
    if mismatched is not None:
        raise WorkflowWorkUnitContractError(
            f"Workflow work unit {definition.key} idempotency key {idempotency_key} was reused with different input."
        )
    existing = session.execute(
        select(WorkflowOperationWorkUnit).where(
            WorkflowOperationWorkUnit.operation_id == operation.operation_id,
            WorkflowOperationWorkUnit.unit_key == definition.key,
            WorkflowOperationWorkUnit.idempotency_key == idempotency_key,
            WorkflowOperationWorkUnit.input_fingerprint == input_fingerprint,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    timestamp = _now()
    work_unit = WorkflowOperationWorkUnit(
        work_unit_id=uuid4().hex,
        operation_id=operation.operation_id,
        parent_attempt_id=parent_attempt.attempt_id,
        unit_key=definition.key,
        unit_kind=definition.kind.value,
        idempotency_key=idempotency_key,
        input_fingerprint=input_fingerprint,
        status=WORK_UNIT_STATUS_RUNNING,
        output_json=None,
        error_category=None,
        error_message=None,
        created_at=timestamp,
        updated_at=timestamp,
        completed_at=None,
    )
    session.add(work_unit)
    session.flush()
    return work_unit


def _start_unit_attempt(
    *,
    session: Session,
    work_unit: WorkflowOperationWorkUnit,
    operation_attempt: WorkflowOperationAttempt,
) -> WorkflowOperationWorkUnitAttempt:
    next_attempt_number = int(
        session.execute(
            select(func.max(WorkflowOperationWorkUnitAttempt.attempt_number)).where(
                WorkflowOperationWorkUnitAttempt.work_unit_id == work_unit.work_unit_id
            )
        ).scalar_one()
        or 0
    ) + 1
    timestamp = _now()
    unit_attempt = WorkflowOperationWorkUnitAttempt(
        work_unit_attempt_id=uuid4().hex,
        work_unit_id=work_unit.work_unit_id,
        operation_attempt_id=operation_attempt.attempt_id,
        attempt_number=next_attempt_number,
        status=WORK_UNIT_STATUS_RUNNING,
        error_category=None,
        error_message=None,
        next_retry_at=None,
        created_at=timestamp,
        started_at=timestamp,
        finished_at=None,
    )
    work_unit.status = WORK_UNIT_STATUS_RUNNING
    work_unit.updated_at = timestamp
    session.add(unit_attempt)
    session.flush()
    return unit_attempt


def _unit_attempt_count_for_operation_attempt(
    *,
    session: Session,
    work_unit: WorkflowOperationWorkUnit,
    operation_attempt: WorkflowOperationAttempt,
) -> int:
    return int(
        session.execute(
            select(func.count(WorkflowOperationWorkUnitAttempt.work_unit_attempt_id)).where(
                WorkflowOperationWorkUnitAttempt.work_unit_id == work_unit.work_unit_id,
                WorkflowOperationWorkUnitAttempt.operation_attempt_id == operation_attempt.attempt_id,
            )
        ).scalar_one()
        or 0
    )


def run_work_unit(
    session: Session,
    *,
    operation: WorkflowOperation,
    operation_attempt: WorkflowOperationAttempt,
    unit_key: str,
    idempotency_key: str,
    input_payload: object,
    execute: Callable[[WorkflowWorkUnitContext], T],
    serialize: Callable[[T], dict[str, object]] | None = None,
    deserialize: Callable[[dict[str, object]], T] | None = None,
) -> T:
    if operation_attempt.operation_id != operation.operation_id:
        raise WorkflowWorkUnitContractError("Workflow work unit attempt does not belong to the supplied operation.")
    workflow, workflow_type, definition = _resolve_definition(session=session, operation=operation, unit_key=unit_key)
    normalized_idempotency_key = _validate_idempotency(definition=definition, idempotency_key=idempotency_key)
    fingerprint = _input_fingerprint(input_payload)
    work_unit = _get_or_create_work_unit(
        session=session,
        operation=operation,
        parent_attempt=operation_attempt,
        definition=definition,
        idempotency_key=normalized_idempotency_key,
        input_fingerprint=fingerprint,
    )
    output_loader = deserialize or _default_deserialize
    if work_unit.status == WORK_UNIT_STATUS_COMPLETED:
        if not isinstance(work_unit.output_json, dict):
            raise WorkflowWorkUnitContractError(f"Completed work unit {unit_key} is missing persisted output.")
        existing_reuse_attempt = session.execute(
            select(WorkflowOperationWorkUnitAttempt)
            .where(
                WorkflowOperationWorkUnitAttempt.work_unit_id == work_unit.work_unit_id,
                WorkflowOperationWorkUnitAttempt.operation_attempt_id == operation_attempt.attempt_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        if existing_reuse_attempt is None:
            reused_attempt = _start_unit_attempt(
                session=session,
                work_unit=work_unit,
                operation_attempt=operation_attempt,
            )
            reused_attempt.status = WORK_UNIT_STATUS_COMPLETED
            reused_attempt.finished_at = _now()
            work_unit.status = WORK_UNIT_STATUS_COMPLETED
        emit_workflow_operation_log(
            session,
            operation=operation,
            attempt_ref=_attempt_ref(operation, operation_attempt),
            event_type="workflow_work_unit_reused",
            message=f"Reused completed work unit {unit_key}.",
            metadata={
                "work_unit_key": unit_key,
                "work_unit_id": work_unit.work_unit_id,
                "work_unit_kind": definition.kind.value,
                "idempotency_key": normalized_idempotency_key,
                "input_fingerprint": fingerprint,
            },
        )
        session.flush()
        return output_loader(dict(work_unit.output_json))

    operation_unit_attempt_count = _unit_attempt_count_for_operation_attempt(
        session=session,
        work_unit=work_unit,
        operation_attempt=operation_attempt,
    )
    if operation_unit_attempt_count >= definition.retry_policy.max_attempts:
        raise WorkflowWorkUnitRetryExhaustedError(
            f"Workflow work unit {unit_key} exhausted {definition.retry_policy.max_attempts} attempts."
        )
    unit_attempt = _start_unit_attempt(session=session, work_unit=work_unit, operation_attempt=operation_attempt)
    context = WorkflowWorkUnitContext(
        workflow=workflow,
        workflow_type=workflow_type,
        operation=operation,
        operation_attempt=operation_attempt,
        definition=definition,
        work_unit=work_unit,
        attempt=unit_attempt,
    )
    emit_workflow_operation_log(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation, operation_attempt),
        event_type="workflow_work_unit_started",
        message=f"Started work unit {unit_key} attempt {unit_attempt.attempt_number}.",
        metadata={
            "work_unit_key": unit_key,
            "work_unit_id": work_unit.work_unit_id,
            "work_unit_attempt_id": unit_attempt.work_unit_attempt_id,
            "work_unit_kind": definition.kind.value,
            "idempotency_key": normalized_idempotency_key,
            "input_fingerprint": fingerprint,
        },
    )
    heartbeat_controller = WorkflowOperationAttemptHeartbeatController(
        database_url=session.get_bind().url.render_as_string(hide_password=False),
        attempt_id=operation_attempt.attempt_id,
        lease_owner="workflow_work_units",
    )
    heartbeat_controller.start()
    try:
        result = execute(context)
    except Exception as exc:
        timestamp = _now()
        message = str(exc) or exc.__class__.__name__
        next_delay = _backoff_seconds(definition=definition, attempt_number=unit_attempt.attempt_number)
        next_retry_at = timestamp + timedelta(seconds=next_delay) if next_delay > 0 else None
        retrying = (
            next_retry_at is not None
            and operation_unit_attempt_count + 1 < definition.retry_policy.max_attempts
        )
        unit_attempt.status = WORK_UNIT_STATUS_RETRYING if retrying else WORK_UNIT_STATUS_FAILED
        unit_attempt.error_category = "work_unit_failure"
        unit_attempt.error_message = message
        unit_attempt.next_retry_at = next_retry_at
        unit_attempt.finished_at = timestamp
        work_unit.status = unit_attempt.status
        work_unit.error_category = unit_attempt.error_category
        work_unit.error_message = message
        work_unit.updated_at = timestamp
        emit_workflow_operation_log(
            session,
            operation=operation,
            attempt_ref=_attempt_ref(operation, operation_attempt),
            event_type="workflow_work_unit_failed",
            message=f"Work unit {unit_key} failed: {message}",
            level=logging.ERROR,
            metadata={
                "work_unit_key": unit_key,
                "work_unit_id": work_unit.work_unit_id,
                "work_unit_attempt_id": unit_attempt.work_unit_attempt_id,
                "work_unit_kind": definition.kind.value,
                "idempotency_key": normalized_idempotency_key,
                "input_fingerprint": fingerprint,
                "next_retry_at": next_retry_at.isoformat() if next_retry_at is not None else None,
            },
        )
        session.flush()
        raise
    finally:
        heartbeat_controller.stop()

    serializer = serialize or _default_serialize
    output_payload = serializer(result)
    if not isinstance(output_payload, dict):
        raise WorkflowWorkUnitContractError(f"Workflow work unit {unit_key} serializer must return a JSON object.")
    timestamp = _now()
    unit_attempt.status = WORK_UNIT_STATUS_COMPLETED
    unit_attempt.finished_at = timestamp
    work_unit.status = WORK_UNIT_STATUS_COMPLETED
    work_unit.output_json = output_payload
    work_unit.error_category = None
    work_unit.error_message = None
    work_unit.updated_at = timestamp
    work_unit.completed_at = timestamp
    emit_workflow_operation_log(
        session,
        operation=operation,
        attempt_ref=_attempt_ref(operation, operation_attempt),
        event_type="workflow_work_unit_completed",
        message=f"Completed work unit {unit_key}.",
        metadata={
            "work_unit_key": unit_key,
            "work_unit_id": work_unit.work_unit_id,
            "work_unit_attempt_id": unit_attempt.work_unit_attempt_id,
            "work_unit_kind": definition.kind.value,
            "idempotency_key": normalized_idempotency_key,
            "input_fingerprint": fingerprint,
        },
    )
    session.flush()
    return result
