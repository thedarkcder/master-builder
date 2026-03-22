from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.core.worker.run_health import (
    cleanup_orphan_run_locks,
    recover_stale_running_runs,
    touch_run_heartbeat,
)
from orchestrator.core.runs import RUN_DEDUPE_SCOPE_ISSUE_EXECUTION
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import AgentLifecycleEvent, Run, RunLock, RunLogEvent, Tenant


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

    def _get_lock(self, session, *, issue_key: str, dedupe_scope: str = RUN_DEDUPE_SCOPE_ISSUE_EXECUTION):
        return session.get(
            RunLock,
            {
                "tenant_id": "tenant-a",
                "issue_key": issue_key,
                "dedupe_scope": dedupe_scope,
            },
        )

    def test_touch_run_heartbeat_updates_only_current_owner(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Run(
                    run_id="run-heartbeat",
                    tenant_id="tenant-a",
                    issue_key="TA-1",
                    issue_summary="heartbeat",
                    issue_description="desc",
                    repo_url="https://github.com/example/a",
                    branch=None,
                    pr_url=None,
                    status="running",
                    last_error=None,
                    plan=None,
                    created_at=now - timedelta(minutes=5),
                    started_at=now - timedelta(minutes=4),
                    last_heartbeat_at=now - timedelta(minutes=3),
                    finished_at=None,
                    worker_service_instance_id="node-a:1234",
                )
            )
            session.commit()

            updated = touch_run_heartbeat(
                session,
                run_id="run-heartbeat",
                worker_service_instance_id="node-a:1234",
                heartbeat_at=now,
            )
            self.assertTrue(updated)
            refreshed = session.get(Run, "run-heartbeat")
            assert refreshed is not None
            assert refreshed.last_heartbeat_at is not None
            self.assertEqual(refreshed.last_heartbeat_at.replace(tzinfo=timezone.utc), now)

            rejected = touch_run_heartbeat(
                session,
                run_id="run-heartbeat",
                worker_service_instance_id="node-b:9999",
                heartbeat_at=now + timedelta(seconds=10),
            )
            self.assertFalse(rejected)
            refreshed = session.get(Run, "run-heartbeat")
            assert refreshed is not None
            assert refreshed.last_heartbeat_at is not None
            self.assertEqual(refreshed.last_heartbeat_at.replace(tzinfo=timezone.utc), now)

    def test_cleanup_orphan_run_locks_removes_terminal_locks(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            running = Run(
                run_id="run-active",
                tenant_id="tenant-a",
                issue_key="TA-2",
                issue_summary="active",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                last_error=None,
                plan=None,
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                finished_at=None,
            )
            terminal = Run(
                run_id="run-terminal",
                tenant_id="tenant-a",
                issue_key="TA-3",
                issue_summary="terminal",
                issue_description="desc",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="failed",
                last_error="boom",
                plan=None,
                created_at=now,
                started_at=now,
                last_heartbeat_at=now,
                finished_at=now,
            )
            session.add_all([running, terminal])
            session.add_all(
                [
                    RunLock(
                        tenant_id="tenant-a",
                        issue_key="TA-2",
                        dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                        run_id="run-active",
                        locked_at=now,
                    ),
                    RunLock(
                        tenant_id="tenant-a",
                        issue_key="TA-3",
                        dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                        run_id="run-terminal",
                        locked_at=now,
                    ),
                ]
            )
            session.commit()

            removed = cleanup_orphan_run_locks(session=session)
            self.assertEqual(removed, 1)
            self.assertIsNotNone(self._get_lock(session, issue_key="TA-2"))
            self.assertIsNone(self._get_lock(session, issue_key="TA-3"))

    def test_recover_stale_running_runs_marks_failed_and_preserves_fresh_runs(self) -> None:
        now = datetime.now(timezone.utc)
        stale_heartbeat = now - timedelta(minutes=20)
        stale_started = now - timedelta(minutes=18)
        fresh_started = now - timedelta(seconds=30)
        with self.session_factory() as session:
            session.add_all(
                [
                    Run(
                        run_id="run-stale-heartbeat",
                        tenant_id="tenant-a",
                        issue_key="TA-10",
                        issue_summary="stale heartbeat",
                        issue_description="desc",
                        repo_url="https://github.com/example/a",
                        branch=None,
                        pr_url=None,
                        status="running",
                        last_error=None,
                        plan=None,
                        created_at=stale_started,
                        started_at=stale_started,
                        last_heartbeat_at=stale_heartbeat,
                        finished_at=None,
                        worker_service_instance_id="node-a:1234",
                    ),
                    Run(
                        run_id="run-stale-no-heartbeat",
                        tenant_id="tenant-a",
                        issue_key="TA-11",
                        issue_summary="stale no heartbeat",
                        issue_description="desc",
                        repo_url="https://github.com/example/a",
                        branch=None,
                        pr_url=None,
                        status="running",
                        last_error=None,
                        plan=None,
                        created_at=stale_started,
                        started_at=stale_started,
                        last_heartbeat_at=None,
                        finished_at=None,
                        worker_service_instance_id="node-b:9999",
                    ),
                    Run(
                        run_id="run-fresh-no-heartbeat",
                        tenant_id="tenant-a",
                        issue_key="TA-12",
                        issue_summary="fresh no heartbeat",
                        issue_description="desc",
                        repo_url="https://github.com/example/a",
                        branch=None,
                        pr_url=None,
                        status="running",
                        last_error=None,
                        plan=None,
                        created_at=fresh_started,
                        started_at=fresh_started,
                        last_heartbeat_at=None,
                        finished_at=None,
                        worker_service_instance_id="node-c:1111",
                    ),
                ]
            )
            session.add_all(
                [
                    RunLock(
                        tenant_id="tenant-a",
                        issue_key="TA-10",
                        dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                        run_id="run-stale-heartbeat",
                        locked_at=stale_started,
                    ),
                    RunLock(
                        tenant_id="tenant-a",
                        issue_key="TA-11",
                        dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                        run_id="run-stale-no-heartbeat",
                        locked_at=stale_started,
                    ),
                    RunLock(
                        tenant_id="tenant-a",
                        issue_key="TA-12",
                        dedupe_scope=RUN_DEDUPE_SCOPE_ISSUE_EXECUTION,
                        run_id="run-fresh-no-heartbeat",
                        locked_at=fresh_started,
                    ),
                ]
            )
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

            self.assertIsNone(self._get_lock(session, issue_key="TA-10"))
            self.assertIsNone(self._get_lock(session, issue_key="TA-11"))
            self.assertIsNotNone(self._get_lock(session, issue_key="TA-12"))

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
