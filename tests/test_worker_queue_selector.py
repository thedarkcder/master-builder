import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from orchestrator.core.worker.queue_selector import (
    QueueClaimabilityReason,
    claim_next_queued_run,
    coerce_positive_int,
    probe_claimable_queued_run,
    select_next_queued_run,
)
from orchestrator.core.runs.service import resolve_required_worker_capability_from_plan
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Tenant, TenantRunClaim
from tests.workflow_test_support import add_run_with_workflow, make_run


def _add_run(session, *, now: datetime, **kwargs) -> None:
    orchestration_backend = kwargs.pop("orchestration_backend", "legacy")
    if "required_worker_capability" not in kwargs and isinstance(kwargs.get("plan"), dict):
        kwargs["required_worker_capability"] = resolve_required_worker_capability_from_plan(kwargs["plan"])
    add_run_with_workflow(
        session,
        make_run(created_at=now, **kwargs),
        orchestration_backend=orchestration_backend,
    )


def _plan_for_capability(required_worker_capability: str) -> dict:
    snapshot = ExecutionSnapshot.empty()
    snapshot.workflow.outcome = "requeue"
    snapshot.workflow.requeue_target = required_worker_capability
    snapshot.workflow.requeue_reason = "Capability-specific worker required"
    return snapshot.dump()


class WorkerQueueSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_queue_selector.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    def test_coerce_positive_int_handles_invalid_and_non_positive(self) -> None:
        self.assertEqual(coerce_positive_int(None, default=2), 2)
        self.assertEqual(coerce_positive_int("bad", default=2), 2)
        self.assertEqual(coerce_positive_int(0, default=2), 1)
        self.assertEqual(coerce_positive_int(-5, default=2), 1)
        self.assertEqual(coerce_positive_int("3", default=2), 3)

    def test_select_next_queued_run_marks_missing_tenant_terminal(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            _add_run(
                session,
                now=now,
                run_id="run-missing-tenant",
                tenant_id="tenant-missing",
                issue_key="MAB-900",
                issue_summary="Missing tenant",
                issue_description="How to test: verify missing tenant handling.",
                repo_url="https://github.com/example/missing",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
            )

            self.assertIsNotNone(result.terminal_run)
            self.assertIsNone(result.run)
            self.assertEqual(result.terminal_run.status, "failed")
            self.assertEqual(result.terminal_run.last_error, "Tenant not found for queued run")
            self.assertIsNotNone(result.terminal_run.finished_at)

    def test_claim_next_queued_run_skips_concurrency_limited_tenant(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Tenant(
                    tenant_id="tenant-b",
                    name="Tenant B",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/b"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-running",
                tenant_id="tenant-a",
                issue_key="MAB-901",
                issue_summary="Tenant A running",
                issue_description="running",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                plan=_plan_for_capability("linux"),
                started_at=now,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-queued",
                tenant_id="tenant-a",
                issue_key="MAB-902",
                issue_summary="Tenant A queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-b-queued",
                tenant_id="tenant-b",
                issue_key="MAB-903",
                issue_summary="Tenant B queued",
                issue_description="queued",
                repo_url="https://github.com/example/b",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            result = claim_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
                worker_service_instance_id="node-a:1234",
            )

            self.assertIsNone(result.terminal_run)
            self.assertIsNotNone(result.claimed_run)
            assert result.claimed_run is not None
            self.assertEqual(result.claimed_run.run_id, "run-b-queued")
            self.assertEqual(result.claimed_run.tenant.tenant_id, "tenant-b")
            self.assertEqual(result.claimed_run.status, "dispatching")
            self.assertEqual(result.claimed_run.worker_service_instance_id, "node-a:1234")
            self.assertTrue(result.claimed_run.claim_id)
            claim_row = session.get(TenantRunClaim, "tenant-b")
            self.assertIsNotNone(claim_row)

    def test_claim_next_queued_run_claims_temporal_backend_runs_for_execution_worker(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 2},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-temporal",
                tenant_id="tenant-a",
                issue_key="MAB-913",
                issue_summary="Temporal queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
                orchestration_backend="temporal",
            )
            session.commit()

            result = claim_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
                worker_service_instance_id="node-a:1234",
                worker_capabilities={"linux"},
            )

            self.assertIsNotNone(result.claimed_run)
            assert result.claimed_run is not None
            self.assertEqual(result.claimed_run.run_id, "run-temporal")
            self.assertEqual(result.claimed_run.status, "dispatching")

    def test_claim_next_queued_run_ignores_stale_running_run_for_concurrency(self) -> None:
        now = datetime.now(timezone.utc)
        stale_heartbeat = now - timedelta(minutes=10)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-running-stale",
                tenant_id="tenant-a",
                issue_key="MAB-920",
                issue_summary="Tenant A stale running",
                issue_description="running",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                plan=_plan_for_capability("linux"),
                started_at=stale_heartbeat,
                finished_at=None,
                last_heartbeat_at=stale_heartbeat,
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-queued-fresh",
                tenant_id="tenant-a",
                issue_key="MAB-921",
                issue_summary="Tenant A queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            result = claim_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
                worker_service_instance_id="node-a:1234",
                running_stale_timeout_seconds=300,
            )

            self.assertIsNotNone(result.claimed_run)
            assert result.claimed_run is not None
            self.assertEqual(result.claimed_run.run_id, "run-a-queued-fresh")
            self.assertEqual(result.claimed_run.status, "dispatching")

    def test_claim_next_queued_run_recovers_when_claim_row_is_inserted_concurrently(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-race",
                    name="Tenant Race",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/race"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-race-queued",
                tenant_id="tenant-race",
                issue_key="MAB-904",
                issue_summary="Tenant race queued",
                issue_description="queued",
                repo_url="https://github.com/example/race",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

        with self.session_factory() as seed_session:
            seed_session.add(TenantRunClaim(tenant_id="tenant-race", updated_at=now))
            seed_session.commit()

        with self.session_factory() as session:
            original_get = session.get

            def _racy_get(model, ident, *args, **kwargs):
                if model is TenantRunClaim and ident == "tenant-race":
                    return None
                return original_get(model, ident, *args, **kwargs)

            with patch.object(session, "get", side_effect=_racy_get):
                result = claim_next_queued_run(
                    session,
                    queued_status="queued",
                    running_status="running",
                    failed_status="failed",
                    worker_service_instance_id="node-a:1234",
                )

            self.assertIsNotNone(result.claimed_run)
            assert result.claimed_run is not None
            self.assertEqual(result.claimed_run.run_id, "run-race-queued")
            self.assertEqual(result.claimed_run.status, "dispatching")

    def test_select_next_queued_run_applies_project_overrides_when_project_id_unset(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 5},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Tenant(
                    tenant_id="tenant-b",
                    name="Tenant B",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/b"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="tenant-a-proj",
                    tenant_id="tenant-a",
                    name="Tenant A Project",
                    github_repository="https://github.com/example/a",
                    jira_project_key="MAB",
                    policy_overrides={"max_concurrent_runs": 1},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-running",
                tenant_id="tenant-a",
                issue_key="MAB-100",
                issue_summary="Tenant A running",
                issue_description="running",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                plan=_plan_for_capability("linux"),
                started_at=now,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-queued-unbound",
                tenant_id="tenant-a",
                project_id=None,
                issue_key="MAB-101",
                issue_summary="Tenant A queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-b-queued",
                tenant_id="tenant-b",
                issue_key="XYZ-1",
                issue_summary="Tenant B queued",
                issue_description="queued",
                repo_url="https://github.com/example/b",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
            )

            self.assertIsNone(result.terminal_run)
            self.assertIsNotNone(result.run)
            self.assertEqual(result.run.run_id, "run-a-queued-unbound")

    def test_select_next_queued_run_skips_incompatible_worker_capabilities(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-capabilities",
                    name="Tenant Capabilities",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/mobile"},
                    policy_config={"max_concurrent_runs": 2},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-macos",
                tenant_id="tenant-capabilities",
                issue_key="IOS-1",
                issue_summary="Build iOS app with SwiftUI",
                issue_description="Implement iOS app shell",
                repo_url="https://github.com/example/mobile",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("macos"),
                started_at=None,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-linux",
                tenant_id="tenant-capabilities",
                issue_key="LINUX-1",
                issue_summary="Build backend service",
                issue_description="Implement API endpoint",
                repo_url="https://github.com/example/backend",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            linux_result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
                worker_capabilities={"linux"},
            )
            self.assertIsNotNone(linux_result.run)
            self.assertEqual(linux_result.run.run_id, "run-linux")

            macos_result = select_next_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                failed_status="failed",
                worker_capabilities={"macos"},
            )
            self.assertIsNotNone(macos_result.run)
            self.assertEqual(macos_result.run.run_id, "run-macos")

    def test_probe_claimable_queued_run_reports_claimable_candidate_after_skipping_limited_tenant(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Tenant(
                    tenant_id="tenant-b",
                    name="Tenant B",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/b"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-running",
                tenant_id="tenant-a",
                issue_key="MAB-910",
                issue_summary="Tenant A running",
                issue_description="running",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                plan=_plan_for_capability("linux"),
                started_at=now,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-queued",
                tenant_id="tenant-a",
                issue_key="MAB-911",
                issue_summary="Tenant A queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-b-queued",
                tenant_id="tenant-b",
                issue_key="MAB-912",
                issue_summary="Tenant B queued",
                issue_description="queued",
                repo_url="https://github.com/example/b",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            probe = probe_claimable_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                worker_capabilities={"linux"},
            )

            self.assertTrue(probe.claimable)
            self.assertEqual(probe.reason, QueueClaimabilityReason.CLAIMABLE)
            self.assertEqual(probe.run_id, "run-b-queued")
            self.assertEqual(probe.tenant_id, "tenant-b")
            self.assertEqual(probe.issue_key, "MAB-912")

    def test_probe_claimable_queued_run_reports_temporal_run_claimable_for_execution_worker(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 2},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-temporal",
                tenant_id="tenant-a",
                issue_key="MAB-915",
                issue_summary="Temporal queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
                orchestration_backend="temporal",
            )
            session.commit()

            probe = probe_claimable_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                worker_capabilities={"linux"},
            )

            self.assertTrue(probe.claimable)
            self.assertEqual(probe.reason, QueueClaimabilityReason.CLAIMABLE)
            self.assertEqual(probe.run_id, "run-temporal")

    def test_probe_claimable_queued_run_ignores_stale_running_run_for_concurrency(self) -> None:
        now = datetime.now(timezone.utc)
        stale_heartbeat = now - timedelta(minutes=10)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-running-stale",
                tenant_id="tenant-a",
                issue_key="MAB-930",
                issue_summary="Tenant A stale running",
                issue_description="running",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                plan=_plan_for_capability("linux"),
                started_at=stale_heartbeat,
                finished_at=None,
                last_heartbeat_at=stale_heartbeat,
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-queued-fresh",
                tenant_id="tenant-a",
                issue_key="MAB-931",
                issue_summary="Tenant A queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            probe = probe_claimable_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                worker_capabilities={"linux"},
                running_stale_timeout_seconds=300,
            )

            self.assertTrue(probe.claimable)
            self.assertEqual(probe.reason, QueueClaimabilityReason.CLAIMABLE)
            self.assertEqual(probe.run_id, "run-a-queued-fresh")

    def test_probe_claimable_queued_run_reports_capability_mismatch_when_no_worker_match(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-capabilities",
                    name="Tenant Capabilities",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/mobile"},
                    policy_config={"max_concurrent_runs": 2},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-macos",
                tenant_id="tenant-capabilities",
                issue_key="IOS-10",
                issue_summary="Build iOS app with SwiftUI",
                issue_description="Implement iOS app shell",
                repo_url="https://github.com/example/mobile",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("macos"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            probe = probe_claimable_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                worker_capabilities={"linux"},
            )

            self.assertFalse(probe.claimable)
            self.assertEqual(probe.reason, QueueClaimabilityReason.CAPABILITY_MISMATCH)
            self.assertEqual(probe.run_id, "run-macos")
            self.assertEqual(probe.tenant_id, "tenant-capabilities")
            self.assertEqual(probe.issue_key, "IOS-10")

    def test_probe_claimable_queued_run_reports_runtime_unavailable_when_required_runtime_is_blocked(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-runtime",
                    name="Tenant Runtime",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/runtime"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-runtime",
                tenant_id="tenant-runtime",
                issue_key="MAB-940",
                issue_summary="Runtime blocked",
                issue_description="queued",
                repo_url="https://github.com/example/runtime",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                required_runtime_kinds_json=["codex_cli"],
                started_at=None,
                finished_at=None,
            )
            session.commit()

            probe = probe_claimable_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                worker_capabilities={"linux"},
                ready_runtime_kinds={"openai"},
            )

            self.assertFalse(probe.claimable)
            self.assertEqual(probe.reason, QueueClaimabilityReason.RUNTIME_UNAVAILABLE)
            self.assertEqual(probe.run_id, "run-runtime")
            self.assertEqual(probe.tenant_id, "tenant-runtime")
            self.assertEqual(probe.issue_key, "MAB-940")

    def test_probe_claimable_queued_run_reports_blocked_candidate_for_concurrency_limit(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={"github_repository": "https://github.com/example/a"},
                    policy_config={"max_concurrent_runs": 1},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-running",
                tenant_id="tenant-a",
                issue_key="MAB-950",
                issue_summary="Tenant A running",
                issue_description="running",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="running",
                plan=_plan_for_capability("linux"),
                started_at=now,
                finished_at=None,
            )
            _add_run(
                session,
                now=now,
                run_id="run-a-queued",
                tenant_id="tenant-a",
                issue_key="MAB-951",
                issue_summary="Tenant A queued",
                issue_description="queued",
                repo_url="https://github.com/example/a",
                branch=None,
                pr_url=None,
                status="queued",
                plan=_plan_for_capability("linux"),
                started_at=None,
                finished_at=None,
            )
            session.commit()

            probe = probe_claimable_queued_run(
                session,
                queued_status="queued",
                running_status="running",
                worker_capabilities={"linux"},
            )

            self.assertFalse(probe.claimable)
            self.assertEqual(probe.reason, QueueClaimabilityReason.CONCURRENCY_LIMIT)
            self.assertEqual(probe.run_id, "run-a-queued")
            self.assertEqual(probe.tenant_id, "tenant-a")
            self.assertEqual(probe.issue_key, "MAB-951")
