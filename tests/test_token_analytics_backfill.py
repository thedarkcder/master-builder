import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from sqlalchemy import select

from orchestrator.api.admin.token_compare_service import compare_run_tokens
from orchestrator.api.admin.token_diagnostics_service import get_token_stage_diagnostics
from orchestrator.api.admin.token_overview_service import get_token_overview
from orchestrator.api.admin.token_timeline_service import get_run_token_timeline
from orchestrator.api.admin.token_usage_backfill import materialize_token_usage_for_scope
from orchestrator.api.schemas import TokenCompareRequest
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Run, RunLogEvent, RunTokenUsage, Tenant


class TokenAnalyticsBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/token_analytics.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(self.database_url)
        self.now = datetime.now(timezone.utc)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _seed_run(self, session, *, run_id: str, issue_key: str) -> None:
        tenant_id = "tenant-a"
        project_id = "tenant-a-default"
        if session.get(Tenant, tenant_id) is None:
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    name="Tenant A",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        if session.get(Project, project_id) is None:
            session.add(
                Project(
                    project_id=project_id,
                    tenant_id=tenant_id,
                    name="Default Project",
                    github_repository="https://github.com/example/repo",
                    jira_project_key="TP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=self.now,
                    updated_at=self.now,
                )
            )
        session.flush()
        session.add(
            Run(
                run_id=run_id,
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key=issue_key,
                issue_summary="summary",
                issue_description="description",
                repo_url="https://github.com/example/repo",
                branch="jira/TP-1",
                pr_url=None,
                status="completed",
                last_error=None,
                plan=None,
                created_at=self.now,
                started_at=self.now,
                finished_at=self.now,
            )
        )

    def _insert_turn_completed_log(
        self,
        session,
        *,
        run_id: str,
        issue_key: str,
        event_id: str,
        invocation_id: str,
        turn_id: str,
        stage: str,
        attempt: int = 1,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
    ) -> None:
        message = (
            '{"type":"turn.completed","turn_id":"%s","runtime_ms":10,'
            '"usage":{"input_tokens":%d,"cached_input_tokens":%d,"output_tokens":%d}}'
            % (turn_id, input_tokens, cached_input_tokens, output_tokens)
        )
        session.add(
            RunLogEvent(
                event_id=event_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id=run_id,
                issue_key=issue_key,
                agent_id="agent-1",
                invocation_id=invocation_id,
                channel="codex",
                command="npm run test",
                working_dir="/workspace",
                stage=stage,
                attempt=attempt,
                stream="stdout",
                message=message,
                recorded_at=self.now,
            )
        )

    def _insert_token_usage_row(
        self,
        session,
        *,
        run_id: str,
        invocation_id: str,
        turn_id: str | None,
        stage: str,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        attempt: int = 1,
        runtime_ms: int | None = 10,
    ) -> None:
        session.add(
            RunTokenUsage(
                run_id=run_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                attempt=attempt,
                stage=stage,
                invocation_id=invocation_id,
                turn_id=turn_id,
                recorded_at=self.now,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                delta_input=None,
                delta_uncached=None,
                delta_output=None,
                runtime_ms=runtime_ms,
                model="gpt-5.4",
                command="npm run test",
                artifact_path=None,
                output_chars=None,
                truncated=None,
            )
        )

    def test_materialize_token_usage_for_scope_is_idempotent(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-1",
                invocation_id="inv-1",
                turn_id="turn-1",
                stage="test",
                input_tokens=120,
                cached_input_tokens=40,
                output_tokens=20,
            )
            session.commit()

        with self.session_factory() as session:
            inserted_first = materialize_token_usage_for_scope(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            inserted_second = materialize_token_usage_for_scope(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()
            usage_rows = session.execute(select(RunTokenUsage)).scalars().all()

        self.assertEqual(inserted_first, 1)
        self.assertEqual(inserted_second, 0)
        self.assertEqual(len(usage_rows), 1)
        self.assertEqual(usage_rows[0].input_tokens, 120)

    def test_materialize_token_usage_for_scope_skips_usage_without_turn_id(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            session.add(
                RunLogEvent(
                    event_id="event-no-turn",
                    tenant_id="tenant-a",
                    project_id="tenant-a-default",
                    run_id="run-1",
                    issue_key="TP-1",
                    agent_id="agent-1",
                    invocation_id="inv-1",
                    channel="codex",
                    command="npm run test",
                    working_dir="/workspace",
                    stage="test",
                    attempt=1,
                    stream="stdout",
                    message='{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":40,"output_tokens":20}}',
                    recorded_at=self.now,
                )
            )
            session.commit()

        with self.session_factory() as session:
            inserted = materialize_token_usage_for_scope(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()
            usage_rows = session.execute(select(RunTokenUsage)).scalars().all()

        self.assertEqual(inserted, 0)
        self.assertEqual(len(usage_rows), 0)

    def test_materialize_token_usage_for_scope_honors_issue_filter(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._seed_run(session, run_id="run-2", issue_key="TP-2")
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-1",
                invocation_id="inv-1",
                turn_id="turn-1",
                stage="test",
                input_tokens=120,
                cached_input_tokens=40,
                output_tokens=20,
            )
            self._insert_turn_completed_log(
                session,
                run_id="run-2",
                issue_key="TP-2",
                event_id="event-2",
                invocation_id="inv-2",
                turn_id="turn-2",
                stage="test",
                input_tokens=220,
                cached_input_tokens=70,
                output_tokens=30,
            )
            session.commit()

        with self.session_factory() as session:
            inserted = materialize_token_usage_for_scope(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-2",
                max_rows=1,
            )
            session.commit()
            rows = session.execute(select(RunTokenUsage)).scalars().all()

        self.assertEqual(inserted, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].run_id, "run-2")

    def test_token_overview_backfills_when_usage_table_is_empty(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-1",
                invocation_id="inv-1",
                turn_id="turn-1",
                stage="dev",
                input_tokens=100,
                cached_input_tokens=60,
                output_tokens=30,
            )
            session.commit()

        with self.session_factory() as session:
            result = get_token_overview(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()
            usage_count = session.execute(select(RunTokenUsage)).scalars().all()

        self.assertEqual(result.kpis.total_input, 100)
        self.assertEqual(result.kpis.total_output, 30)
        self.assertGreaterEqual(result.kpis.avg_io_per_run, 0.0)
        self.assertGreaterEqual(result.kpis.p95_io_per_run, 0.0)
        self.assertGreaterEqual(result.kpis.retest_waste_score, 0.0)
        self.assertEqual(len(usage_count), 1)

    def test_token_compare_backfills_scoped_runs(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._seed_run(session, run_id="run-2", issue_key="TP-2")
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-1",
                invocation_id="inv-1",
                turn_id="turn-1",
                stage="test",
                input_tokens=140,
                cached_input_tokens=70,
                output_tokens=40,
            )
            self._insert_turn_completed_log(
                session,
                run_id="run-2",
                issue_key="TP-2",
                event_id="event-2",
                invocation_id="inv-2",
                turn_id="turn-2",
                stage="test",
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=25,
            )
            session.commit()

        with self.session_factory() as session:
            response = compare_run_tokens(
                session=session,
                payload=TokenCompareRequest(run_ids=["run-1", "run-2"]),
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()
            usage_count = session.execute(select(RunTokenUsage)).scalars().all()

        self.assertEqual(len(response.runs), 2)
        self.assertEqual(len(response.waterfall), 2)
        self.assertEqual(len(usage_count), 2)

    def test_timeline_and_diagnostics_backfill(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-1",
                invocation_id="inv-1",
                turn_id="turn-1",
                stage="review",
                input_tokens=110,
                cached_input_tokens=50,
                output_tokens=35,
            )
            session.commit()

        with self.session_factory() as session:
            timeline = get_run_token_timeline(
                session=session,
                run_id="run-1",
                tenant_id="tenant-a",
            )
            diagnostics = get_token_stage_diagnostics(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()

        self.assertEqual(len(timeline.turns), 1)
        self.assertEqual(timeline.totals.input, 110)
        review_stage = next((item for item in diagnostics.stages if item.stage == "review"), None)
        self.assertIsNotNone(review_stage)
        self.assertEqual(review_stage.run_count if review_stage else 0, 1)
        self.assertGreaterEqual(diagnostics.retest_waste_score, 0.0)
        self.assertEqual(len(diagnostics.heatmap), 1)
        self.assertEqual(len(diagnostics.issue_stage_totals), 1)
        self.assertEqual(diagnostics.issue_stage_totals[0].issue_key, "TP-1")
        self.assertEqual(diagnostics.issue_stage_totals[0].stage, "review")

    def test_timeline_ignores_rows_without_turn_id(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="strict-invocation",
                turn_id="turn-1",
                stage="review",
                input_tokens=110,
                cached_input_tokens=50,
                output_tokens=35,
            )
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="snapshot-invocation",
                turn_id=None,
                stage="review",
                input_tokens=5_000,
                cached_input_tokens=4_000,
                output_tokens=500,
            )
            session.commit()

        with self.session_factory() as session:
            timeline = get_run_token_timeline(
                session=session,
                run_id="run-1",
                tenant_id="tenant-a",
            )
            session.commit()

        self.assertEqual(len(timeline.turns), 1)
        self.assertEqual(timeline.totals.input, 110)
        self.assertEqual(timeline.totals.output, 35)

    def test_token_overview_ignores_rows_without_turn_id(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="strict-invocation",
                turn_id="turn-1",
                stage="dev",
                input_tokens=100,
                cached_input_tokens=60,
                output_tokens=30,
            )
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="snapshot-invocation",
                turn_id=None,
                stage="dev",
                input_tokens=4_000,
                cached_input_tokens=3_500,
                output_tokens=400,
            )
            session.commit()

        with self.session_factory() as session:
            overview = get_token_overview(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()

        self.assertEqual(overview.kpis.total_input, 100)
        self.assertEqual(overview.kpis.total_output, 30)
        self.assertEqual(len(overview.top_costly_runs), 1)
        self.assertEqual(overview.top_costly_runs[0].input, 100)

    def test_token_compare_ignores_rows_without_turn_id(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._seed_run(session, run_id="run-2", issue_key="TP-2")
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="strict-invocation-1",
                turn_id="turn-1",
                stage="test",
                input_tokens=140,
                cached_input_tokens=70,
                output_tokens=40,
            )
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="snapshot-invocation-1",
                turn_id=None,
                stage="test",
                input_tokens=8_000,
                cached_input_tokens=7_000,
                output_tokens=800,
            )
            self._insert_token_usage_row(
                session,
                run_id="run-2",
                invocation_id="strict-invocation-2",
                turn_id="turn-2",
                stage="test",
                input_tokens=80,
                cached_input_tokens=20,
                output_tokens=25,
            )
            session.commit()

        with self.session_factory() as session:
            response = compare_run_tokens(
                session=session,
                payload=TokenCompareRequest(run_ids=["run-1", "run-2"]),
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()

        self.assertEqual(len(response.waterfall), 2)
        run_one = next(item for item in response.runs if item.run_id == "run-1")
        self.assertEqual(run_one.totals.input, 140)
        self.assertEqual(run_one.totals.output, 40)

    def test_token_diagnostics_ignores_rows_without_turn_id(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="strict-invocation",
                turn_id="turn-1",
                stage="review",
                input_tokens=110,
                cached_input_tokens=50,
                output_tokens=35,
            )
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="snapshot-invocation",
                turn_id=None,
                stage="review",
                input_tokens=9_000,
                cached_input_tokens=8_000,
                output_tokens=900,
            )
            session.commit()

        with self.session_factory() as session:
            diagnostics = get_token_stage_diagnostics(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()

        review_stage = next(item for item in diagnostics.stages if item.stage == "review")
        self.assertEqual(review_stage.avg_delta, 110.0)
        self.assertEqual(len(diagnostics.issue_stage_totals), 1)
        self.assertEqual(diagnostics.issue_stage_totals[0].total_io, 145)

    def test_only_with_test_stage_requires_strict_test_turns(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._seed_run(session, run_id="run-2", issue_key="TP-2")
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="strict-dev",
                turn_id="dev-turn-1",
                stage="dev",
                input_tokens=100,
                cached_input_tokens=60,
                output_tokens=20,
            )
            self._insert_token_usage_row(
                session,
                run_id="run-1",
                invocation_id="snapshot-test",
                turn_id=None,
                stage="test",
                input_tokens=4_000,
                cached_input_tokens=3_000,
                output_tokens=400,
            )
            self._insert_token_usage_row(
                session,
                run_id="run-2",
                invocation_id="strict-test",
                turn_id="test-turn-1",
                stage="test",
                input_tokens=150,
                cached_input_tokens=70,
                output_tokens=30,
            )
            session.commit()

        with self.session_factory() as session:
            diagnostics = get_token_stage_diagnostics(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                only_with_test_stage=True,
            )
            overview = get_token_overview(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                only_with_test_stage=True,
            )
            session.commit()

        self.assertEqual([item.run_id for item in overview.top_costly_runs], ["run-2"])
        self.assertEqual([item.issue_key for item in diagnostics.issue_stage_totals], ["TP-2"])

    def test_retest_waste_score_grows_when_test_retries_without_dev_attempt(self) -> None:
        with self.session_factory() as session:
            self._seed_run(session, run_id="run-1", issue_key="TP-1")
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-dev-1",
                invocation_id="inv-dev",
                turn_id="turn-dev-1",
                stage="dev",
                input_tokens=100,
                cached_input_tokens=40,
                output_tokens=30,
            )
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-test-1",
                invocation_id="inv-test",
                turn_id="turn-test-1",
                stage="test",
                input_tokens=120,
                cached_input_tokens=50,
                output_tokens=20,
            )
            self._insert_turn_completed_log(
                session,
                run_id="run-1",
                issue_key="TP-1",
                event_id="event-test-2",
                invocation_id="inv-test",
                turn_id="turn-test-2",
                stage="test",
                attempt=2,
                input_tokens=220,
                cached_input_tokens=70,
                output_tokens=20,
            )
            session.commit()

        with self.session_factory() as session:
            diagnostics = get_token_stage_diagnostics(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            overview = get_token_overview(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
            )
            session.commit()

        self.assertGreater(diagnostics.retest_waste_score, 0.0)
        self.assertGreater(overview.kpis.retest_waste_score, 0.0)


if __name__ == "__main__":
    unittest.main()
