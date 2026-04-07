from __future__ import annotations

import os
import unittest
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet
from sqlalchemy import select

from orchestrator.core.run_logs import record_run_log_event
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import RunLogEvent, RunStreamEvent


class RunLogRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/run_log_redaction.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        reset_db_engine_cache()

    def test_record_run_log_event_persists_redacted_messages(self) -> None:
        message = (
            "run_id=123e4567-e89b-12d3-a456-426614174000 "
            "APP_STORE_CONNECT_API_KEY_BASE64=super-secret-value "
            "email=user@example.com "
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
        )
        with self.session_factory() as session:
            record_run_log_event(
                session=session,
                tenant_id="tenant-a",
                project_id="project-a",
                run_id="run-1",
                issue_key="TA-1",
                agent_id="worker-1",
                invocation_id="inv-1",
                channel="worker",
                command="workflow.test",
                working_dir="/tmp/repo",
                stage="test",
                attempt=1,
                stream="stderr",
                message=message,
            )
            session.commit()

        with self.session_factory() as session:
            log_row = session.execute(select(RunLogEvent)).scalar_one()
            stream_row = session.execute(select(RunStreamEvent)).scalar_one()

        for persisted in (log_row.message, stream_row.message):
            self.assertNotIn("super-secret-value", persisted)
            self.assertNotIn("user@example.com", persisted)
            self.assertNotIn("BEGIN PRIVATE KEY", persisted)
            self.assertIn("123e4567-e89b-12d3-a456-426614174000", persisted)
            self.assertIn("[REDACTED]", persisted)


if __name__ == "__main__":
    unittest.main()
