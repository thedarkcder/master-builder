from __future__ import annotations

from dataclasses import dataclass
import unittest
from unittest.mock import patch

from orchestrator.core.workflow.execution_snapshot_migration import ExecutionSnapshotMigrationReport
from orchestrator.core.workflow.execution_snapshot_startup import (
    ensure_execution_snapshot_startup_bootstrap,
    run_execution_snapshot_startup_bootstrap,
)


@dataclass
class _FakeSession:
    calls: list[tuple[str, object]]

    def execute(self, statement, params=None):  # noqa: ANN001, ANN201
        self.calls.append((str(statement), params))
        return None

    def commit(self) -> None:
        self.calls.append(("commit", None))


class _FakeSessionContext:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001, ANN201
        return False


class _FakeSessionFactory:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self):  # noqa: ANN204
        return _FakeSessionContext(self._session)


def _ok_report() -> ExecutionSnapshotMigrationReport:
    return ExecutionSnapshotMigrationReport(
        scanned_runs=1,
        converted_runs=0,
        invalid_runs=0,
        scanned_checkpoints=1,
        converted_checkpoints=0,
        invalid_checkpoints=0,
        invalid_run_ids=(),
        invalid_checkpoint_ids=(),
    )


class ExecutionSnapshotStartupBootstrapTests(unittest.TestCase):
    def test_startup_bootstrap_skips_non_postgres(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        with patch(
            "orchestrator.core.workflow.execution_snapshot_startup.is_postgres_database_url",
            side_effect=lambda _: False,
        ):
            report = run_execution_snapshot_startup_bootstrap(
                session_factory=factory,  # type: ignore[arg-type]
                database_url="sqlite:///tmp/test.db",
                actor="api",
            )

        self.assertIsNone(report)
        self.assertEqual(session.calls, [])

    def test_startup_bootstrap_runs_migration_under_advisory_lock(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        with (
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup.is_postgres_database_url",
                side_effect=lambda _: True,
            ),
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup.migrate_execution_snapshots",
                side_effect=lambda **_: _ok_report(),
            ),
        ):
            report = run_execution_snapshot_startup_bootstrap(
                session_factory=factory,  # type: ignore[arg-type]
                database_url="postgresql://user:pass@localhost/db",
                actor="worker",
            )

        self.assertIsNotNone(report)
        self.assertTrue(any("pg_advisory_lock" in stmt for stmt, _ in session.calls))
        self.assertTrue(any("pg_advisory_unlock" in stmt for stmt, _ in session.calls))
        self.assertEqual(session.calls[-1][0], "commit")

    def test_startup_bootstrap_logs_and_returns_report_on_invalid_rows(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        invalid_report = ExecutionSnapshotMigrationReport(
            scanned_runs=1,
            converted_runs=0,
            invalid_runs=1,
            scanned_checkpoints=1,
            converted_checkpoints=0,
            invalid_checkpoints=0,
            invalid_run_ids=("run-1",),
            invalid_checkpoint_ids=(),
        )
        with (
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup.is_postgres_database_url",
                side_effect=lambda _: True,
            ),
            patch(
                "orchestrator.core.workflow.execution_snapshot_startup.migrate_execution_snapshots",
                side_effect=lambda **_: invalid_report,
            ),
        ):
            with self.assertLogs(
                "orchestrator.core.workflow.execution_snapshot_startup",
                level="ERROR",
            ) as captured:
                report = run_execution_snapshot_startup_bootstrap(
                    session_factory=factory,  # type: ignore[arg-type]
                    database_url="postgresql://user:pass@localhost/db",
                    actor="api",
                )

        self.assertEqual(report, invalid_report)
        self.assertTrue(any("execution_snapshot_startup_bootstrap_invalid_rows" in line for line in captured.output))
        self.assertEqual(session.calls[-1][0], "commit")

    def test_ensure_startup_bootstrap_passes_when_report_clean(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        with patch(
            "orchestrator.core.workflow.execution_snapshot_startup.run_execution_snapshot_startup_bootstrap",
            return_value=_ok_report(),
        ):
            ensure_execution_snapshot_startup_bootstrap(
                session_factory=factory,  # type: ignore[arg-type]
                database_url="postgresql://user:pass@localhost/db",
                actor="api",
            )

    def test_ensure_startup_bootstrap_raises_on_invalid_rows(self) -> None:
        session = _FakeSession(calls=[])
        factory = _FakeSessionFactory(session)
        invalid_report = ExecutionSnapshotMigrationReport(
            scanned_runs=1,
            converted_runs=0,
            invalid_runs=1,
            scanned_checkpoints=1,
            converted_checkpoints=0,
            invalid_checkpoints=1,
            invalid_run_ids=("run-1",),
            invalid_checkpoint_ids=("cp-1",),
        )
        with patch(
            "orchestrator.core.workflow.execution_snapshot_startup.run_execution_snapshot_startup_bootstrap",
            return_value=invalid_report,
        ):
            with self.assertRaises(RuntimeError):
                ensure_execution_snapshot_startup_bootstrap(
                    session_factory=factory,  # type: ignore[arg-type]
                    database_url="postgresql://user:pass@localhost/db",
                    actor="worker",
                )

if __name__ == "__main__":
    unittest.main()
