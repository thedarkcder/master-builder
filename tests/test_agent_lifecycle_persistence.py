import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from sqlalchemy import select

from orchestrator.core.agent_observability import prune_agent_lifecycle_events, record_agent_lifecycle_event
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import AgentLifecycleEvent, RunStreamEvent


class AgentLifecyclePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/agent_lifecycle.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(self.database_url)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def test_record_agent_lifecycle_event_persists_to_shared_storage(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            record_agent_lifecycle_event(
                session=session,
                event_type="TASK_STARTED",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id="run-1",
                issue_key="TP-1",
                agent_id="worker-1",
                recorded_at=now,
            )
            session.commit()

        with self.session_factory() as session:
            events = session.execute(select(AgentLifecycleEvent)).scalars().all()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].tenant_id, "tenant-a")
            self.assertEqual(events[0].event_type, "TASK_STARTED")

    def test_prune_agent_lifecycle_event_applies_retention_cap(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            for idx in range(6):
                record_agent_lifecycle_event(
                    session=session,
                    event_type="TASK_STARTED",
                    tenant_id="tenant-a",
                    project_id="tenant-a-default",
                    run_id=f"run-{idx}",
                    issue_key=f"TP-{idx}",
                    agent_id="worker-1",
                    recorded_at=now + timedelta(seconds=idx),
                )
            prune_agent_lifecycle_events(session=session, max_events_per_tenant=3)
            session.commit()

        with self.session_factory() as session:
            events = session.execute(
                select(AgentLifecycleEvent)
                .where(AgentLifecycleEvent.tenant_id == "tenant-a")
                .order_by(AgentLifecycleEvent.recorded_at.asc())
            ).scalars().all()
            self.assertEqual(len(events), 3)
            self.assertEqual([event.run_id for event in events], ["run-3", "run-4", "run-5"])

            stream_events = session.execute(
                select(RunStreamEvent)
                .where(
                    RunStreamEvent.tenant_id == "tenant-a",
                    RunStreamEvent.event_kind == "agent_lifecycle",
                )
                .order_by(RunStreamEvent.stream_offset.asc())
            ).scalars().all()
            self.assertEqual(len(stream_events), 3)
            self.assertEqual([event.run_id for event in stream_events], ["run-3", "run-4", "run-5"])


if __name__ == "__main__":
    unittest.main()
