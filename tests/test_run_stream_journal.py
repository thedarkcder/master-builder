import json
import os
import threading
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import select

from orchestrator.core.agent_observability import record_agent_lifecycle_event
from orchestrator.core.config import get_settings
from orchestrator.core.log_event_bus import RunStreamBroker, build_run_stream_matcher, encode_stream_row, shutdown_run_streaming
from orchestrator.core.run_logs import record_run_log_event
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import RunStreamEvent, RunTokenUsage


class RunStreamJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/run_stream.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ["ORCHESTRATOR_LOG_BUS_ENABLED"] = "true"
        os.environ.pop("ORCHESTRATOR_REDIS_URL", None)
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(self.database_url)

    def tearDown(self) -> None:
        shutdown_run_streaming()
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        os.environ.pop("ORCHESTRATOR_LOG_BUS_ENABLED", None)
        os.environ.pop("ORCHESTRATOR_REDIS_URL", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def test_commit_enqueues_best_effort_redis_wakeup_after_commit(self) -> None:
        shutdown_run_streaming()
        os.environ["ORCHESTRATOR_REDIS_URL"] = "redis://127.0.0.1:6379/0"
        get_settings.cache_clear()
        published: list[tuple[int, int, int]] = []
        publish_event = threading.Event()

        def _record_publish(message) -> None:
            published.append((message.min_offset, message.max_offset, message.count))
            publish_event.set()

        with (
            patch("orchestrator.core.log_event_bus._redis_wakeups_enabled", return_value=True),
            patch("orchestrator.core.log_event_bus._publish_wakeup", side_effect=_record_publish),
        ):
            with self.session_factory() as session:
                record_run_log_event(
                    session=session,
                    tenant_id="tenant-a",
                    project_id="project-a",
                    run_id="run-publish",
                    issue_key="TP-4",
                    agent_id="worker-1",
                    invocation_id="inv-publish",
                    channel="worker",
                    command="workflow.dev",
                    working_dir="/tmp/repo",
                    stage="dev",
                    attempt=1,
                    stream="stdout",
                    message="wake me",
                )
                session.commit()

            self.assertTrue(publish_event.wait(timeout=2.0))
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][2], 1)

    def test_recorders_dual_write_to_run_stream_journal_and_token_usage(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            record_agent_lifecycle_event(
                session=session,
                event_type="TASK_STARTED",
                tenant_id="tenant-a",
                project_id="project-a",
                run_id="run-1",
                issue_key="TP-1",
                agent_id="worker-1",
                recorded_at=now,
            )
            record_run_log_event(
                session=session,
                tenant_id="tenant-a",
                project_id="project-a",
                run_id="run-1",
                issue_key="TP-1",
                agent_id="worker-1",
                invocation_id="inv-1",
                channel="worker",
                command="workflow.dev",
                working_dir="/tmp/repo",
                stage="dev",
                attempt=1,
                stream="stdout",
                message=json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 11,
                            "cached_input_tokens": 3,
                            "output_tokens": 7,
                        },
                        "turn_id": "turn-1",
                    }
                ),
                recorded_at=now,
            )
            session.commit()

        with self.session_factory() as session:
            rows = session.execute(
                select(RunStreamEvent).order_by(RunStreamEvent.stream_offset.asc())
            ).scalars().all()
            self.assertEqual([row.event_kind for row in rows], ["agent_lifecycle", "codex_log"])
            self.assertEqual(rows[0].event_type, "TASK_STARTED")
            self.assertEqual(rows[1].command, "workflow.dev")
            token_rows = session.execute(select(RunTokenUsage)).scalars().all()
            self.assertEqual(len(token_rows), 1)
            self.assertEqual(token_rows[0].input_tokens, 11)
            self.assertEqual(token_rows[0].cached_input_tokens, 3)
            self.assertEqual(token_rows[0].output_tokens, 7)

    def test_broker_catches_up_without_redis_delivery(self) -> None:
        broker = RunStreamBroker()
        broker.start()
        subscriber = broker.subscribe(
            subscriber_id="sub-1",
            buffer_size=8,
            match_fn=build_run_stream_matcher(run_id="run-2"),
            render_fn=encode_stream_row,
        )
        try:
            with self.session_factory() as session:
                record_run_log_event(
                    session=session,
                    tenant_id="tenant-a",
                    project_id="project-a",
                    run_id="run-2",
                    issue_key="TP-2",
                    agent_id="worker-1",
                    invocation_id="inv-2",
                    channel="worker",
                    command="workflow.dev",
                    working_dir="/tmp/repo",
                    stage="dev",
                    attempt=1,
                    stream="stdout",
                    message="hello world",
                )
                session.commit()

            broker.request_catchup()
            message = subscriber.queue.get(timeout=2.0)
            self.assertIn('"run_id":"run-2"', message)
            self.assertIn('"message":"hello world"', message)
        finally:
            broker.stop()

    def test_broker_disconnects_slow_subscriber_on_overflow(self) -> None:
        broker = RunStreamBroker()
        broker.start()
        subscriber = broker.subscribe(
            subscriber_id="slow-sub",
            buffer_size=1,
            match_fn=build_run_stream_matcher(run_id="run-3"),
            render_fn=encode_stream_row,
        )
        try:
            with self.session_factory() as session:
                for idx in range(3):
                    record_run_log_event(
                        session=session,
                        tenant_id="tenant-a",
                        project_id="project-a",
                        run_id="run-3",
                        issue_key="TP-3",
                        agent_id="worker-1",
                        invocation_id="inv-3",
                        channel="worker",
                        command="workflow.dev",
                        working_dir="/tmp/repo",
                        stage="dev",
                        attempt=1,
                        stream="stdout",
                        message=f"log-{idx}",
                    )
                session.commit()

            broker.request_catchup()
            first = subscriber.queue.get(timeout=2.0)
            self.assertIn('"run_id":"run-3"', first)
            self.assertTrue(subscriber.disconnected)
        finally:
            broker.stop()


if __name__ == "__main__":
    unittest.main()
