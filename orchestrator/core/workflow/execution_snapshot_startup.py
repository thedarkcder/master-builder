from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.run_queue_events import is_postgres_database_url
from orchestrator.storage.models import Run, WorkflowCheckpoint

logger = logging.getLogger(__name__)

_EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY = 740_002_611


@dataclass(frozen=True)
class ExecutionSnapshotStartupReport:
    scanned_runs: int
    invalid_runs: int
    scanned_checkpoints: int
    invalid_checkpoints: int
    invalid_run_ids: tuple[str, ...]
    invalid_checkpoint_ids: tuple[str, ...]


def run_execution_snapshot_startup_bootstrap(
    *,
    session_factory: sessionmaker[Session],
    database_url: str,
    actor: str,
) -> ExecutionSnapshotStartupReport | None:
    if not is_postgres_database_url(database_url):
        logger.info(
            "execution_snapshot_startup_bootstrap_skipped actor=%s reason=non_postgres",
            actor,
        )
        return None

    with session_factory() as session:
        session.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": _EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY},
        )
        try:
            report = _validate_execution_snapshots(session=session)
        finally:
            session.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": _EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY},
            )
            session.commit()

    if report.invalid_runs or report.invalid_checkpoints:
        logger.error(
            "execution_snapshot_startup_bootstrap_invalid_rows "
            "actor=%s invalid_runs=%s invalid_checkpoints=%s invalid_run_ids=%s invalid_checkpoint_ids=%s",
            actor,
            report.invalid_runs,
            report.invalid_checkpoints,
            list(report.invalid_run_ids),
            list(report.invalid_checkpoint_ids),
        )
        return report

    logger.info(
        "execution_snapshot_startup_bootstrap_valid actor=%s scanned_runs=%s scanned_checkpoints=%s",
        actor,
        report.scanned_runs,
        report.scanned_checkpoints,
    )
    return report


def ensure_execution_snapshot_startup_bootstrap(
    *,
    session_factory: sessionmaker[Session],
    database_url: str,
    actor: str,
) -> None:
    report = run_execution_snapshot_startup_bootstrap(
        session_factory=session_factory,
        database_url=database_url,
        actor=actor,
    )
    if report is None:
        return
    if report.invalid_runs or report.invalid_checkpoints:
        raise RuntimeError(
            "Execution snapshot startup validation found incompatible rows; "
            f"invalid_runs={report.invalid_runs} invalid_checkpoints={report.invalid_checkpoints}"
        )


def _validate_execution_snapshots(*, session: Session) -> ExecutionSnapshotStartupReport:
    scanned_runs = 0
    invalid_runs = 0
    invalid_run_ids: list[str] = []

    run_statement = select(Run.run_id, Run.plan).order_by(Run.created_at.asc(), Run.run_id.asc())
    for run_id, payload in session.execute(run_statement):
        scanned_runs += 1
        if payload is None:
            continue
        if ExecutionSnapshot.load(payload) is None:
            invalid_runs += 1
            invalid_run_ids.append(str(run_id))

    scanned_checkpoints = 0
    invalid_checkpoints = 0
    invalid_checkpoint_ids: list[str] = []

    checkpoint_statement = select(
        WorkflowCheckpoint.checkpoint_id,
        WorkflowCheckpoint.payload_json,
    ).order_by(WorkflowCheckpoint.created_at.asc(), WorkflowCheckpoint.checkpoint_id.asc())
    for checkpoint_id, payload in session.execute(checkpoint_statement):
        scanned_checkpoints += 1
        if payload is None:
            invalid_checkpoints += 1
            invalid_checkpoint_ids.append(str(checkpoint_id))
            continue
        if ExecutionSnapshot.load(payload) is None:
            invalid_checkpoints += 1
            invalid_checkpoint_ids.append(str(checkpoint_id))

    return ExecutionSnapshotStartupReport(
        scanned_runs=scanned_runs,
        invalid_runs=invalid_runs,
        scanned_checkpoints=scanned_checkpoints,
        invalid_checkpoints=invalid_checkpoints,
        invalid_run_ids=tuple(invalid_run_ids),
        invalid_checkpoint_ids=tuple(invalid_checkpoint_ids),
    )
