from __future__ import annotations

from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import Run, WorkflowCheckpoint


@dataclass(frozen=True)
class ExecutionSnapshotMigrationReport:
    scanned_runs: int
    converted_runs: int
    invalid_runs: int
    scanned_checkpoints: int
    converted_checkpoints: int
    invalid_checkpoints: int
    invalid_run_ids: tuple[str, ...]
    invalid_checkpoint_ids: tuple[str, ...]


def migrate_execution_snapshots(
    session: Session,
    *,
    apply: bool,
    limit: int | None = None,
) -> ExecutionSnapshotMigrationReport:
    run_statement = select(Run).order_by(Run.created_at.asc(), Run.run_id.asc())
    checkpoint_statement = select(WorkflowCheckpoint).order_by(
        WorkflowCheckpoint.created_at.asc(),
        WorkflowCheckpoint.checkpoint_id.asc(),
    )
    if isinstance(limit, int) and limit > 0:
        run_statement = run_statement.limit(limit)
        checkpoint_statement = checkpoint_statement.limit(limit)

    scanned_runs = 0
    converted_runs = 0
    invalid_runs = 0
    invalid_run_ids: list[str] = []

    for run in session.execute(run_statement).scalars().all():
        scanned_runs += 1
        payload = getattr(run, "plan", None)
        if payload is None:
            continue
        canonical = canonicalize_snapshot_payload(payload)
        if canonical is None:
            invalid_runs += 1
            invalid_run_ids.append(str(run.run_id))
            continue
        if ExecutionSnapshot.load(payload) is not None:
            continue
        converted_runs += 1
        if apply:
            run.plan = canonical.dump()

    scanned_checkpoints = 0
    converted_checkpoints = 0
    invalid_checkpoints = 0
    invalid_checkpoint_ids: list[str] = []

    for checkpoint in session.execute(checkpoint_statement).scalars().all():
        scanned_checkpoints += 1
        payload = getattr(checkpoint, "payload_json", None)
        canonical = canonicalize_snapshot_payload(payload)
        if canonical is None:
            invalid_checkpoints += 1
            invalid_checkpoint_ids.append(str(checkpoint.checkpoint_id))
            continue
        if ExecutionSnapshot.load(payload) is not None:
            continue
        converted_checkpoints += 1
        if apply:
            checkpoint.payload_json = canonical.dump()

    if apply:
        session.commit()
    else:
        session.rollback()

    return ExecutionSnapshotMigrationReport(
        scanned_runs=scanned_runs,
        converted_runs=converted_runs,
        invalid_runs=invalid_runs,
        scanned_checkpoints=scanned_checkpoints,
        converted_checkpoints=converted_checkpoints,
        invalid_checkpoints=invalid_checkpoints,
        invalid_run_ids=tuple(invalid_run_ids),
        invalid_checkpoint_ids=tuple(invalid_checkpoint_ids),
    )


def canonicalize_snapshot_payload(payload: object | None) -> ExecutionSnapshot | None:
    if payload is None:
        return None
    loaded = ExecutionSnapshot.load(payload)
    if loaded is not None:
        return loaded
    if not isinstance(payload, dict):
        return None
    if not payload:
        return ExecutionSnapshot.empty()
    return None
