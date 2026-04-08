from __future__ import annotations

import os
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest

from orchestrator.core.config import get_settings
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.execution_snapshot_migration import (
    canonicalize_snapshot_payload,
    migrate_execution_snapshots,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Run, Tenant, WorkflowCheckpoint, WorkflowExecution


class ExecutionSnapshotMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/snapshot_migration_test.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._seed_fixture_rows()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _seed_fixture_rows(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                WorkflowExecution(
                    workflow_id="workflow-1",
                    tenant_id="tenant-1",
                    project_id=None,
                    issue_key="TP-1",
                    issue_summary="Summary",
                    issue_description="Description",
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    dedupe_scope="issue_execution",
                    status="queued",
                    last_error=None,
                    active_run_id="run-1",
                    latest_checkpoint_id="run-1-execution",
                    source_workflow_id=None,
                    source_run_id=None,
                    blocked_reason=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="run-1",
                    workflow_id="workflow-1",
                    tenant_id="tenant-1",
                    project_id=None,
                    issue_key="TP-1",
                    issue_summary="Summary",
                    issue_description="Description",
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    attempt_number=1,
                    parent_run_id=None,
                    entry_mode="fresh",
                    entry_stage="orchestrated",
                    entry_checkpoint_id=None,
                    dedupe_scope="issue_execution",
                    status="queued",
                    last_error=None,
                    plan={
                        "trigger_context": {"source": "manual"},
                        "pre_check": {"outcome": "ready_for_agent"},
                    },
                    created_at=now,
                    started_at=None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None,
                )
            )
            session.add(
                WorkflowCheckpoint(
                    checkpoint_id="run-1-execution",
                    workflow_id="workflow-1",
                    run_id="run-1",
                    checkpoint_kind="execution",
                    stage="execution",
                    payload_json={
                        "trigger_context": {"source": "manual"},
                    },
                    codex_session_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_canonicalize_snapshot_payload_rejects_non_dict_payloads(self) -> None:
        self.assertIsNone(canonicalize_snapshot_payload(None))
        self.assertIsNone(canonicalize_snapshot_payload("invalid"))

    def test_canonicalize_snapshot_payload_converts_legacy_trigger_context(self) -> None:
        snapshot = canonicalize_snapshot_payload(
            {
                "trigger_context": {"source": "manual_fix_request", "pr_number": 42},
                "pre_check": {"outcome": "ready_for_agent"},
            }
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        dumped = snapshot.dump()
        self.assertEqual(
            dumped["context"]["trigger_context"],
            {"source": "manual_fix_request", "pr_number": 42},
        )
        self.assertEqual(
            dumped["context"]["execution_context"]["pre_check_outcome"],
            "ready_for_agent",
        )

    def test_migrate_execution_snapshots_dry_run_reports_conversions_without_persisting(self) -> None:
        with self.session_factory() as session:
            report = migrate_execution_snapshots(session=session, apply=False)
            self.assertEqual(report.converted_runs, 1)
            self.assertEqual(report.converted_checkpoints, 1)

        with self.session_factory() as session:
            run = session.get(Run, "run-1")
            assert run is not None
            self.assertEqual(run.plan["trigger_context"]["source"], "manual")

    def test_migrate_execution_snapshots_apply_persists_canonical_shape(self) -> None:
        with self.session_factory() as session:
            report = migrate_execution_snapshots(session=session, apply=True)
            self.assertEqual(report.invalid_runs, 0)
            self.assertEqual(report.invalid_checkpoints, 0)
            self.assertEqual(report.converted_runs, 1)
            self.assertEqual(report.converted_checkpoints, 1)

        with self.session_factory() as session:
            run = session.get(Run, "run-1")
            checkpoint = session.get(WorkflowCheckpoint, "run-1-execution")
            assert run is not None
            assert checkpoint is not None
            self.assertIsNotNone(ExecutionSnapshot.load(run.plan))
            self.assertIsNotNone(ExecutionSnapshot.load(checkpoint.payload_json))


if __name__ == "__main__":
    unittest.main()
