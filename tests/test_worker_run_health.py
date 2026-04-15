from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.core.worker.run_health import (
    recover_stale_running_runs,
    touch_run_heartbeat,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import AgentLifecycleEvent, Run, RunLogEvent, Tenant, WorkflowExecution
from tests.workflow_test_support import add_run_with_workflow, make_run


class WorkerRunHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_run_health.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._seed_tenant()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    def _seed_tenant(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={"project_keys": ["TA"]},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def _get_workflow(self, session, *, issue_key: str):
        return session.execute(
            select(WorkflowExecution).where(
                WorkflowExecution.tenant_id == "tenant-a",
                WorkflowExecution.issue_key == issue_key,
                WorkflowExecution.dedupe_scope == "issue_execution",
            )
        ).scalar_one_or_none()

    def test_touch_run_heartbeat_updates_only_current_owner(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            add_run_with_workflow(
                session,
                make_run(
                    run_id="run-heartbeat",
                    tenant_id="tenant-a",
                    issue_key="TA-1",
                    issue_summary="heartbeat",
                    issue_description="desc",
                    repo_url="https://github.com/example/a",
                    created_at=now - timedelta(minutes=5),
                    status="running",
                    started_at=now - timedelta(minutes=4),
                    last_heartbeat_at=now - timedelta(minutes=3),
                    worker_service_instance_id="node-a:1234",
                    claim_id="claim-1",
                ),
                workflow_status="running",
            )
            session.commit()

            updated = touch_run_heartbeat(
                session,
                run_id="run-heartbeat",
                worker_service_instance_id="node-a:1234",
                claim_id="claim-1",
                heartbeat_at=now,
            )
            self.assertTrue(updated)
            refreshed = session.get(Run, "run-heartbeat")
            assert refreshed is not None
            assert refreshed.last_heartbeat_at is not None
            self.assertEqual(refreshed.last_heartbeat_at.replace(tzinfo=timezone.utc), now)

    def test_touch_run_heartbeat_rejects_dispatching_runs(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            add_run_with_workflow(
                session,
                make_run(
                    run_id="run-dispatching-heartbeat",
                    tenant_id="tenant-a",
                    issue_key="TA-2",
                    issue_summary="dispatching heartbeat",
                    issue_description="desc",
                    repo_url="https://github.com/example/a",
                    created_at=now - timedelta(minutes=5),
                    status="dispatching",
                    started_at=None,
                    dispatch_claimed_at=now - timedelta(minutes=4),
                    last_heartbeat_at=None,
                    worker_service_instance_id="node-a:1234",
                    claim_id="claim-1",
                ),
                workflow_status="queued",
            )
            session.commit()

            updated = touch_run_heartbeat(
                session,
                run_id="run-dispatching-heartbeat",
                worker_service_instance_id="node-a:1234",
                claim_id="claim-1",
                heartbeat_at=now,
            )

            self.assertFalse(updated)
            refreshed = session.get(Run, "run-dispatching-heartbeat")
            assert refreshed is not None
            self.assertIsNone(refreshed.last_heartbeat_at)

    def test_recover_stale_running_runs_marks_failed_and_preserves_fresh_runs(self) -> None:
        now = datetime.now(timezone.utc)
        stale_heartbeat = now - timedelta(minutes=20)
        stale_started = now - timedelta(minutes=18)
        fresh_started = now - timedelta(seconds=30)
        with self.session_factory() as session:
            for run in (
                make_run(
                    run_id="run-stale-heartbeat",
                    tenant_id="tenant-a",
                    issue_key="TA-10",
                    issue_summary="stale heartbeat",
                    issue_description="desc",
                    repo_url="https://github.com/example/a",
                    created_at=stale_started,
                    status="running",
                    started_at=stale_started,
                    last_heartbeat_at=stale_heartbeat,
                    worker_service_instance_id="node-a:1234",
                ),
                make_run(
                    run_id="run-stale-no-heartbeat",
                    tenant_id="tenant-a",
                    issue_key="TA-11",
                    issue_summary="stale no heartbeat",
                    issue_description="desc",
                    repo_url="https://github.com/example/a",
                    created_at=stale_started,
                    status="running",
                    started_at=stale_started,
                    worker_service_instance_id="node-b:9999",
                ),
                make_run(
                    run_id="run-fresh-no-heartbeat",
                    tenant_id="tenant-a",
                    issue_key="TA-12",
                    issue_summary="fresh no heartbeat",
                    issue_description="desc",
                    repo_url="https://github.com/example/a",
                    created_at=fresh_started,
                    status="running",
                    started_at=fresh_started,
                    worker_service_instance_id="node-c:1111",
                ),
            ):
                add_run_with_workflow(session, run, workflow_status="running")
            session.commit()

            recovered = recover_stale_running_runs(
                session=session,
                settings=SimpleNamespace(worker_run_stale_timeout_seconds=300),
                recovered_by_agent_id="worker",
                recovered_by_service_instance_id="node-z:7777",
                now=now,
            )

            self.assertEqual({item.run_id for item in recovered}, {"run-stale-heartbeat", "run-stale-no-heartbeat"})

            stale_run = session.get(Run, "run-stale-heartbeat")
            assert stale_run is not None
            self.assertEqual(stale_run.status, "failed")
            self.assertIsNotNone(stale_run.finished_at)
            self.assertIsNone(stale_run.worker_service_instance_id)
            self.assertIn("previous_owner=node-a:1234", stale_run.last_error or "")

            legacy_run = session.get(Run, "run-stale-no-heartbeat")
            assert legacy_run is not None
            self.assertEqual(legacy_run.status, "failed")
            self.assertIsNone(legacy_run.worker_service_instance_id)

            fresh_run = session.get(Run, "run-fresh-no-heartbeat")
            assert fresh_run is not None
            self.assertEqual(fresh_run.status, "running")

            stale_workflow = self._get_workflow(session, issue_key="TA-10")
            legacy_workflow = self._get_workflow(session, issue_key="TA-11")
            fresh_workflow = self._get_workflow(session, issue_key="TA-12")
            assert stale_workflow is not None
            assert legacy_workflow is not None
            assert fresh_workflow is not None
            self.assertEqual(stale_workflow.status, "failed")
            self.assertEqual(legacy_workflow.status, "failed")
            self.assertEqual(fresh_workflow.status, "running")

            stale_logs = session.execute(
                select(RunLogEvent).where(RunLogEvent.run_id == "run-stale-heartbeat")
            ).scalars().all()
            self.assertEqual(len(stale_logs), 1)
            self.assertEqual(stale_logs[0].command, "workflow.stale_recovery")

            stale_events = session.execute(
                select(AgentLifecycleEvent).where(AgentLifecycleEvent.run_id == "run-stale-heartbeat")
            ).scalars().all()
            self.assertEqual({event.event_type for event in stale_events}, {"RUN_FAILED", "TASK_FAILED"})


if __name__ == "__main__":
    unittest.main()
