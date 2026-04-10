from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.workflow.execution_snapshot_migration import (
    ExecutionSnapshotMigrationReport,
    migrate_execution_snapshots,
)
from orchestrator.storage.run_queue_events import is_postgres_database_url

logger = logging.getLogger(__name__)

_EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY = 740_002_611


def run_execution_snapshot_startup_bootstrap(
    *,
    session_factory: sessionmaker[Session],
    database_url: str,
    actor: str,
) -> ExecutionSnapshotMigrationReport | None:
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
            report = migrate_execution_snapshots(
                session=session,
                apply=True,
                limit=None,
            )
        finally:
            session.execute(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": _EXECUTION_SNAPSHOT_MIGRATION_LOCK_KEY},
            )
            session.commit()

    if report.invalid_runs or report.invalid_checkpoints:
        raise RuntimeError(
            "Execution snapshot startup migration failed: "
            f"invalid_runs={report.invalid_runs} invalid_checkpoints={report.invalid_checkpoints} "
            f"invalid_run_ids={list(report.invalid_run_ids)} "
            f"invalid_checkpoint_ids={list(report.invalid_checkpoint_ids)}"
        )

    logger.info(
        "execution_snapshot_startup_bootstrap_applied actor=%s scanned_runs=%s converted_runs=%s scanned_checkpoints=%s converted_checkpoints=%s",
        actor,
        report.scanned_runs,
        report.converted_runs,
        report.scanned_checkpoints,
        report.converted_checkpoints,
    )
    return report
