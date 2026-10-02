from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.storage.run_queue_events import (
    RUN_QUEUE_NOTIFY_CHANNEL,
    is_postgres_database_url,
    notify_run_enqueued,
    postgres_dsn_from_database_url,
)


class RunQueueEventsTests(unittest.TestCase):
    def test_is_postgres_database_url(self) -> None:
        self.assertTrue(is_postgres_database_url("postgresql://user:pass@localhost/db"))
        self.assertFalse(is_postgres_database_url("sqlite:///tmp/test.db"))
        self.assertFalse(is_postgres_database_url("not-a-url"))

    def test_postgres_dsn_from_database_url(self) -> None:
        dsn = postgres_dsn_from_database_url(
            "postgresql+psycopg://user:pass@localhost:5432/db"
        )
        self.assertTrue(dsn.startswith("postgresql://"))
        self.assertIn("user:pass", dsn)
        with self.assertRaisesRegex(ValueError, "requires a PostgreSQL"):
            postgres_dsn_from_database_url("sqlite:///tmp/test.db")

    def test_notify_run_enqueued_non_postgres_noop(self) -> None:
        session = MagicMock()
        session.get_bind.return_value = MagicMock(dialect=MagicMock(name="sqlite"))
        session.get_bind.return_value.dialect.name = "sqlite"

        notify_run_enqueued(
            session,
            tenant_id="t1",
            project_id="p1",
            run_id="r1",
        )

        session.execute.assert_not_called()

    def test_notify_run_enqueued_postgres_executes_pg_notify(self) -> None:
        session = MagicMock()
        session.get_bind.return_value = MagicMock(dialect=MagicMock(name="postgresql"))
        session.get_bind.return_value.dialect.name = "postgresql"

        notify_run_enqueued(
            session,
            tenant_id="t1",
            project_id="p1",
            run_id="r1",
        )

        session.execute.assert_called_once()
        params = session.execute.call_args.args[1]
        self.assertEqual(params["channel"], RUN_QUEUE_NOTIFY_CHANNEL)
        payload = json.loads(params["payload"])
        self.assertEqual(payload["tenant_id"], "t1")
        self.assertEqual(payload["project_id"], "p1")
        self.assertEqual(payload["run_id"], "r1")
        self.assertNotIn("subject_key", payload)
        self.assertNotIn("issue_key", payload)

    def test_notify_run_enqueued_rejects_empty_run_id(self) -> None:
        session = MagicMock()

        with self.assertRaisesRegex(ValueError, "requires run_id"):
            notify_run_enqueued(
                session,
                tenant_id="t1",
                project_id="p1",
                run_id=" ",
            )

        session.execute.assert_not_called()

    def test_notify_run_enqueued_logs_failures(self) -> None:
        session = MagicMock()
        session.get_bind.return_value = MagicMock(dialect=MagicMock(name="postgresql"))
        session.get_bind.return_value.dialect.name = "postgresql"
        session.execute.side_effect = RuntimeError("pg notify failed")

        with patch(
            "orchestrator.storage.run_queue_events.logger.exception"
        ) as logger_mock:
            notify_run_enqueued(
                session,
                tenant_id="t1",
                project_id=None,
                run_id="r1",
            )

        logger_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
