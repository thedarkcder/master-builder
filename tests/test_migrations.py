import json
import os
import unittest
from collections import Counter
from datetime import datetime, timezone
import importlib.util
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import MagicMock, patch

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy import text

from orchestrator.core.config import get_settings
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.migrations import run_migrations


class MigrationTests(unittest.TestCase):
    def _load_migration_module(self, filename: str, module_name: str):
        root = Path(__file__).resolve().parents[1]
        path = root / "orchestrator" / "storage" / "migrations" / "versions" / filename
        spec = importlib.util.spec_from_file_location(module_name, path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _alembic_upgrade(self, database_url: str, revision: str) -> None:
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.attributes["configure_logger"] = False
        config.set_main_option(
            "script_location",
            str(root / "orchestrator" / "storage" / "migrations"),
        )
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, revision)

    def test_revision_graph_has_single_unique_head(self) -> None:
        root = Path(__file__).resolve().parents[1]
        config = Config(str(root / "alembic.ini"))
        config.attributes["configure_logger"] = False
        config.set_main_option(
            "script_location",
            str(root / "orchestrator" / "storage" / "migrations"),
        )
        script = ScriptDirectory.from_config(config)

        revision_ids: list[str] = []
        for path in sorted((root / "orchestrator" / "storage" / "migrations" / "versions").glob("*.py")):
            contents = path.read_text(encoding="utf-8")
            for line in contents.splitlines():
                if line.startswith("revision = "):
                    revision_ids.append(line.split("=", maxsplit=1)[1].strip().strip('"'))
                    break

        duplicates = {revision_id: count for revision_id, count in Counter(revision_ids).items() if count > 1}
        self.assertEqual(duplicates, {})
        self.assertEqual(script.get_heads(), ["20260430_0098"])

    def test_optional_discord_projection_status_repair_migration(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'discord-projection-repair.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260428_0097')"))
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operations (
                            operation_id VARCHAR PRIMARY KEY,
                            operation_type VARCHAR NOT NULL,
                            status VARCHAR NOT NULL,
                            summary TEXT,
                            finished_at DATETIME,
                            updated_at DATETIME
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operation_attempts (
                            attempt_id VARCHAR PRIMARY KEY,
                            operation_id VARCHAR NOT NULL,
                            attempt_number INTEGER NOT NULL,
                            status VARCHAR NOT NULL,
                            error_category VARCHAR,
                            error_message TEXT,
                            status_detail TEXT,
                            retryable BOOLEAN NOT NULL,
                            next_retry_at DATETIME,
                            finished_at DATETIME
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_operations (
                            operation_id, operation_type, status, summary, finished_at, updated_at
                        )
                        VALUES (
                            'operation-discord', 'discord_followup_projection', 'failed',
                            'Optional Discord clarification projection did not run for MAB-243',
                            NULL, '2026-04-30 09:29:04'
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_operation_attempts (
                            attempt_id, operation_id, attempt_number, status, error_category, error_message,
                            status_detail, retryable, next_retry_at, finished_at
                        )
                        VALUES (
                            'attempt-discord-3', 'operation-discord', 3, 'failed', 'contract_violation',
                            'Optional Discord clarification projection did not run for MAB-243',
                            'old failure', 1, NULL, '2026-04-30 09:29:04'
                        )
                        """
                    )
                )

            self._alembic_upgrade(database_url, "20260430_0098")

            with engine.connect() as connection:
                operation = connection.execute(
                    text("SELECT status, summary FROM workflow_operations WHERE operation_id = 'operation-discord'")
                ).one()
                attempt = connection.execute(
                    text(
                        """
                        SELECT status, error_category, error_message, status_detail, retryable
                        FROM workflow_operation_attempts
                        WHERE attempt_id = 'attempt-discord-3'
                        """
                    )
                ).one()

            self.assertEqual(operation.status, "completed")
            self.assertEqual(operation.summary, "Optional Discord clarification follow-up was not created.")
            self.assertEqual(attempt.status, "completed")
            self.assertIsNone(attempt.error_category)
            self.assertIsNone(attempt.error_message)
            self.assertIsNone(attempt.status_detail)
            self.assertFalse(attempt.retryable)

    def test_code_inferred_workflow_graph_migration_removes_db_authored_definitions(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'workflow-code-graph.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_types (
                            workflow_type_key VARCHAR PRIMARY KEY,
                            orchestration_backend VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_type_operations (
                            workflow_type_key VARCHAR NOT NULL,
                            operation_type VARCHAR NOT NULL,
                            label VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_executions (
                            workflow_id VARCHAR PRIMARY KEY,
                            workflow_type_key VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO workflow_types (workflow_type_key, orchestration_backend) "
                        "VALUES ('parent_planning', 'temporal')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO workflow_type_operations (workflow_type_key, operation_type, label) "
                        "VALUES ('parent_planning', 'brief_normalization', 'Brief normalization')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO workflow_executions (workflow_id, workflow_type_key) "
                        "VALUES ('parent_planning:MAB-233', 'parent_planning')"
                    )
                )

            module = self._load_migration_module(
                "20260428_0096_code_inferred_workflow_graph.py",
                "migration_20260428_0096_code_inferred_workflow_graph",
            )

            def drop_table(table_name: str) -> None:
                connection.execute(text(f"DROP TABLE {table_name}"))

            with (
                engine.begin() as connection,
                patch.object(module.op, "get_bind", return_value=connection),
                patch.object(module.op, "drop_table", side_effect=drop_table),
            ):
                module.upgrade()

            inspector = inspect(engine)
            self.assertNotIn("workflow_types", inspector.get_table_names())
            self.assertNotIn("workflow_type_operations", inspector.get_table_names())
            with engine.connect() as connection:
                row = connection.execute(
                    text("SELECT workflow_type_key FROM workflow_executions WHERE workflow_id = 'parent_planning:MAB-233'")
                ).scalar_one()
            self.assertEqual(row, "parent_planning")

    def test_projection_attempt_history_cleanup_removes_projection_only_attempt_rows(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'projection-attempt-cleanup.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operations (
                            operation_id VARCHAR PRIMARY KEY,
                            operation_type VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operation_attempts (
                            attempt_id VARCHAR PRIMARY KEY,
                            operation_id VARCHAR NOT NULL,
                            attempt_number INTEGER NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE audit_events (
                            event_id VARCHAR PRIMARY KEY,
                            operation_id VARCHAR NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE observability_stream_events (
                            event_id VARCHAR PRIMARY KEY,
                            operation_id VARCHAR NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_operations (operation_id, operation_type) VALUES
                            ('op-brief', 'brief_normalization'),
                            ('op-parent', 'jira_parent_update'),
                            ('op-fanout', 'jira_child_fanout')
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_operation_attempts (attempt_id, operation_id, attempt_number) VALUES
                            ('attempt-brief', 'op-brief', 1),
                            ('attempt-parent', 'op-parent', 1),
                            ('attempt-fanout', 'op-fanout', 1)
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO audit_events (event_id, operation_id) VALUES
                            ('audit-brief', 'op-brief'),
                            ('audit-parent', 'op-parent'),
                            ('audit-fanout', 'op-fanout')
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO observability_stream_events (event_id, operation_id) VALUES
                            ('stream-brief', 'op-brief'),
                            ('stream-parent', 'op-parent'),
                            ('stream-fanout', 'op-fanout')
                        """
                    )
                )
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260422_0089')"))

            self._alembic_upgrade(database_url, "20260422_0090")

            with engine.begin() as connection:
                attempts = connection.execute(
                    text("SELECT attempt_id FROM workflow_operation_attempts ORDER BY attempt_id")
                ).scalars().all()
                audit_ids = connection.execute(
                    text("SELECT event_id FROM audit_events ORDER BY event_id")
                ).scalars().all()
                stream_ids = connection.execute(
                    text("SELECT event_id FROM observability_stream_events ORDER BY event_id")
                ).scalars().all()

            self.assertEqual(attempts, ["attempt-fanout"])
            self.assertEqual(audit_ids, ["audit-fanout"])
            self.assertEqual(stream_ids, ["stream-fanout"])

    def test_attempt_scoped_observability_migration_backfills_deletes_and_constrains_rows(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'attempt-observability.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operation_attempts (
                            attempt_id VARCHAR PRIMARY KEY,
                            operation_id VARCHAR NOT NULL,
                            attempt_number INTEGER NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE audit_events (
                            event_id VARCHAR PRIMARY KEY,
                            operation_id VARCHAR NULL,
                            attempt_id VARCHAR NULL,
                            payload_json JSON NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE observability_stream_events (
                            stream_offset INTEGER PRIMARY KEY AUTOINCREMENT,
                            operation_id VARCHAR NULL,
                            attempt_id VARCHAR NULL,
                            payload_json JSON NOT NULL,
                            recorded_at DATETIME NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_operation_attempts (attempt_id, operation_id, attempt_number) VALUES
                            ('attempt-1', 'op-1', 1)
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO audit_events (event_id, operation_id, attempt_id, payload_json) VALUES
                            ('audit-backfill', 'op-1', NULL, '{"attempt": 1}'),
                            ('audit-delete', 'op-2', NULL, '{}'),
                            ('audit-workflow', NULL, NULL, '{}')
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO observability_stream_events (operation_id, attempt_id, payload_json) VALUES
                            ('op-1', NULL, '{"attempt": 1}'),
                            ('op-2', NULL, '{}'),
                            (NULL, NULL, '{}')
                        """
                    )
                )
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260424_0091')"))

            self._alembic_upgrade(database_url, "20260424_0092")

            with engine.begin() as connection:
                audit_rows = connection.execute(
                    text("SELECT event_id, attempt_id FROM audit_events ORDER BY event_id")
                ).all()
                stream_rows = connection.execute(
                    text("SELECT operation_id, attempt_id FROM observability_stream_events ORDER BY stream_offset")
                ).all()
                self.assertEqual(audit_rows, [("audit-backfill", "attempt-1"), ("audit-workflow", None)])
                self.assertEqual(stream_rows, [("op-1", "attempt-1"), (None, None)])
                with self.assertRaises(Exception):
                    connection.execute(
                        text(
                            """
                            INSERT INTO audit_events (event_id, operation_id, attempt_id, payload_json)
                            VALUES ('audit-invalid', 'op-1', NULL, '{}')
                            """
                        )
                    )

    def test_rename_jira_oauth_platform_secrets_migration_updates_managed_secret_refs(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'atlassian-secret-rename.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE managed_secrets (
                            secret_ref VARCHAR(255) PRIMARY KEY,
                            value_encrypted TEXT NOT NULL,
                            created_at DATETIME NOT NULL,
                            updated_at DATETIME NOT NULL
                        )
                        """
                    )
                )
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    text(
                        """
                        INSERT INTO managed_secrets (secret_ref, value_encrypted, created_at, updated_at)
                        VALUES
                            ('platform/JIRA_OAUTH_CLIENT_ID', 'enc-id', :now, :now),
                            ('platform/JIRA_OAUTH_CLIENT_SECRET', 'enc-secret', :now, :now)
                        """
                    ),
                    {"now": now},
                )
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260422_0088')"))

            self._alembic_upgrade(database_url, "20260422_0089")

            with engine.begin() as connection:
                refs = connection.execute(
                    text("SELECT secret_ref FROM managed_secrets ORDER BY secret_ref")
                ).scalars().all()

            self.assertEqual(
                refs,
                [
                    "platform/ATLASSIAN_OAUTH_CLIENT_ID",
                    "platform/ATLASSIAN_OAUTH_CLIENT_SECRET",
                ],
            )

    def test_audit_events_migration_creates_append_only_table(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'audit-events.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE tenants (
                            tenant_id VARCHAR PRIMARY KEY
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE projects (
                            project_id VARCHAR PRIMARY KEY
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_executions (
                            workflow_id VARCHAR PRIMARY KEY
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE runs (
                            run_id VARCHAR PRIMARY KEY
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operations (
                            operation_id VARCHAR PRIMARY KEY
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operation_attempts (
                            attempt_id VARCHAR PRIMARY KEY
                        )
                        """
                    )
                )
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260420_0083')"))

            self._alembic_upgrade(database_url, "20260420_0084")

            inspector = inspect(engine)
            columns = {column["name"] for column in inspector.get_columns("audit_events")}
            self.assertTrue(
                {
                    "event_id",
                    "tenant_id",
                    "workflow_id",
                    "operation_id",
                    "event_kind",
                    "level",
                    "message",
                    "payload_json",
                    "recorded_at",
                }.issubset(columns)
            )

    def test_failed_attempt_retryability_backfill_marks_failed_attempts_retryable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'workflow-attempt-retryable.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_operation_attempts (
                            attempt_id VARCHAR PRIMARY KEY,
                            operation_id VARCHAR NOT NULL,
                            attempt_number INTEGER NOT NULL,
                            status VARCHAR NOT NULL,
                            retryable BOOLEAN NOT NULL DEFAULT 0
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_operation_attempts (
                            attempt_id,
                            operation_id,
                            attempt_number,
                            status,
                            retryable
                        ) VALUES
                            ('attempt-failed', 'operation-1', 1, 'failed', 0),
                            ('attempt-retrying', 'operation-1', 2, 'retrying', 0),
                            ('attempt-running', 'operation-1', 3, 'running', 0),
                            ('attempt-waiting', 'operation-1', 4, 'waiting_for_input', 0)
                        """
                    )
                )
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260420_0082')"))

            self._alembic_upgrade(database_url, "20260420_0083")

            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        """
                        SELECT attempt_id, retryable
                        FROM workflow_operation_attempts
                        ORDER BY attempt_id
                        """
                    )
                ).mappings().all()

            retryable_by_attempt = {row["attempt_id"]: bool(row["retryable"]) for row in rows}
            self.assertTrue(retryable_by_attempt["attempt-failed"])
            self.assertTrue(retryable_by_attempt["attempt-retrying"])
            self.assertFalse(retryable_by_attempt["attempt-running"])
            self.assertFalse(retryable_by_attempt["attempt-waiting"])

    def test_workflow_execution_public_id_migration_backfills_unique_execution_ids(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'workflow-execution-id.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_executions (
                            workflow_id VARCHAR PRIMARY KEY,
                            workflow_type_key VARCHAR NOT NULL,
                            tenant_id VARCHAR NOT NULL,
                            issue_key VARCHAR NOT NULL,
                            orchestration_backend VARCHAR NOT NULL,
                            dedupe_scope VARCHAR NOT NULL,
                            status VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_executions (
                            workflow_id,
                            workflow_type_key,
                            tenant_id,
                            issue_key,
                            orchestration_backend,
                            dedupe_scope,
                            status
                        ) VALUES
                            ('workflow-1', 'issue_execution', 'tenant-a', 'TP-1', 'temporal', 'issue_execution', 'failed'),
                            ('workflow-2', 'issue_execution', 'tenant-a', 'TP-2', 'temporal', 'issue_execution', 'queued')
                        """
                    )
                )
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260420_0080')"))

            self._alembic_upgrade(database_url, "20260420_0081")

            inspector = inspect(engine)
            columns = {column["name"]: column for column in inspector.get_columns("workflow_executions")}
            self.assertIn("execution_id", columns)
            self.assertFalse(columns["execution_id"]["nullable"])

            indexes = {index["name"]: index for index in inspector.get_indexes("workflow_executions")}
            self.assertIn("ix_workflow_executions_execution_id", indexes)
            self.assertTrue(indexes["ix_workflow_executions_execution_id"]["unique"])

            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        """
                        SELECT workflow_id, execution_id
                        FROM workflow_executions
                        ORDER BY workflow_id
                        """
                    )
                ).mappings().all()
            execution_ids = [str(row["execution_id"] or "").strip() for row in rows]
            self.assertEqual(len(execution_ids), 2)
            self.assertEqual(len(set(execution_ids)), 2)
            self.assertTrue(all(execution_ids))

    def test_workflow_execution_public_id_migration_replaces_ids_copied_from_workflow_keys(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'workflow-execution-id-fix.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_executions (
                            workflow_id VARCHAR PRIMARY KEY,
                            execution_id VARCHAR,
                            workflow_type_key VARCHAR NOT NULL,
                            tenant_id VARCHAR NOT NULL,
                            issue_key VARCHAR NOT NULL,
                            orchestration_backend VARCHAR NOT NULL,
                            dedupe_scope VARCHAR NOT NULL,
                            status VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO workflow_executions (
                            workflow_id,
                            execution_id,
                            workflow_type_key,
                            tenant_id,
                            issue_key,
                            orchestration_backend,
                            dedupe_scope,
                            status
                        ) VALUES
                            ('parent_planning:MAB-215', 'parent_planning:MAB-215', 'parent_planning', 'tenant-a', 'MAB-215', 'temporal', 'parent_planning', 'failed'),
                            ('workflow-2', 'public-2', 'issue_execution', 'tenant-a', 'TP-2', 'temporal', 'issue_execution', 'queued')
                        """
                    )
                )
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260420_0080')"))

            self._alembic_upgrade(database_url, "20260420_0081")

            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        """
                        SELECT workflow_id, execution_id
                        FROM workflow_executions
                        ORDER BY workflow_id
                        """
                    )
                ).mappings().all()

            self.assertEqual(rows[0]["workflow_id"], "parent_planning:MAB-215")
            self.assertNotEqual(rows[0]["execution_id"], "parent_planning:MAB-215")
            self.assertEqual(rows[1]["execution_id"], "public-2")

    def test_workflow_execution_backend_backfill_migration_aligns_execution_rows(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'workflow-backfill.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_types (
                            workflow_type_key VARCHAR PRIMARY KEY,
                            orchestration_backend VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE workflow_executions (
                            workflow_id VARCHAR PRIMARY KEY,
                            workflow_type_key VARCHAR NOT NULL,
                            orchestration_backend VARCHAR NOT NULL
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO workflow_types (workflow_type_key, orchestration_backend) "
                        "VALUES ('issue_execution', 'temporal')"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO workflow_executions (workflow_id, workflow_type_key, orchestration_backend) "
                        "VALUES ('workflow-1', 'issue_execution', 'legacy')"
                    )
                )

            module = self._load_migration_module(
                "20260420_0078_backfill_workflow_execution_backends.py",
                "migration_20260420_0078_backfill_workflow_execution_backends",
            )

            with engine.begin() as connection, patch.object(module.op, "get_bind", return_value=connection):
                module.upgrade()

            with engine.connect() as connection:
                row = connection.execute(
                    text(
                        "SELECT orchestration_backend FROM workflow_executions WHERE workflow_id = 'workflow-1'"
                    )
                ).mappings().one()
            self.assertEqual(row["orchestration_backend"], "temporal")

    def test_architecture_documents_migration_tolerates_preexisting_project_config_column(self) -> None:
        with TemporaryDirectory() as tmpdir:
            database_url = f"sqlite:///{Path(tmpdir) / 'architecture-documents.sqlite'}"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("CREATE TABLE tenants (tenant_id VARCHAR PRIMARY KEY)"))
                connection.execute(
                    text(
                        """
                        CREATE TABLE projects (
                            project_id VARCHAR PRIMARY KEY,
                            architecture_docs_config JSON NOT NULL DEFAULT '{}'
                        )
                        """
                    )
                )
                connection.execute(text("CREATE TABLE knowledge_assets (asset_id VARCHAR PRIMARY KEY)"))
                connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
                connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('20260421_0086')"))

            self._alembic_upgrade(database_url, "20260421_0087")

            inspector = inspect(engine)
            project_columns = {column["name"] for column in inspector.get_columns("projects")}
            self.assertIn("architecture_docs_config", project_columns)
            architecture_columns = {column["name"] for column in inspector.get_columns("architecture_documents")}
            self.assertTrue(
                {
                    "document_id",
                    "tenant_id",
                    "project_id",
                    "parent_issue_key",
                    "provider",
                    "title",
                    "status",
                    "canonical_url",
                }.issubset(architecture_columns)
            )
            indexes = {index["name"] for index in inspector.get_indexes("architecture_documents")}
            self.assertIn("ix_architecture_documents_scope", indexes)
            self.assertIn("ix_architecture_documents_project_status", indexes)

    def test_parent_planning_rename_migration_drops_discovered_workflow_foreign_keys(self) -> None:
        module = self._load_migration_module(
            "20260417_0071_parent_planning_type_rename_and_promotion.py",
            "migration_20260417_0071_drop_fks",
        )

        bind = MagicMock()
        bind.dialect.name = "postgresql"

        class FakeInspector:
            def get_table_names(self):
                return ["runs", "workflow_operations", "workflow_executions", "other_table"]

            def get_foreign_keys(self, table_name):
                mapping = {
                    "runs": [
                        {
                            "name": "fk_runs_workflow_ref",
                            "referred_table": "workflow_executions",
                            "referred_columns": ["workflow_id"],
                            "constrained_columns": ["workflow_id"],
                        }
                    ],
                    "workflow_operations": [
                        {
                            "name": "workflow_operations_workflow_id_fkey",
                            "referred_table": "workflow_executions",
                            "referred_columns": ["workflow_id"],
                            "constrained_columns": ["workflow_id"],
                        }
                    ],
                    "workflow_executions": [
                        {
                            "name": "workflow_executions_source_workflow_id_fkey",
                            "referred_table": "workflow_executions",
                            "referred_columns": ["workflow_id"],
                            "constrained_columns": ["source_workflow_id"],
                        }
                    ],
                    "other_table": [
                        {
                            "name": "other_table_tenant_id_fkey",
                            "referred_table": "tenants",
                            "referred_columns": ["tenant_id"],
                            "constrained_columns": ["tenant_id"],
                        }
                    ],
                }
                return mapping.get(table_name, [])

        op_mock = MagicMock()
        with patch.object(module, "op", op_mock), patch.object(module.sa, "inspect", return_value=FakeInspector()):
            module._drop_workflow_id_foreign_keys(bind)

        dropped = [call.args[:2] for call in op_mock.drop_constraint.call_args_list]
        self.assertEqual(
            dropped,
            [
                ("fk_runs_workflow_ref", "runs"),
                ("workflow_operations_workflow_id_fkey", "workflow_operations"),
                ("workflow_executions_source_workflow_id_fkey", "workflow_executions"),
            ],
        )

    def test_parent_planning_rename_migration_recreates_missing_canonical_workflow_foreign_keys(self) -> None:
        module = self._load_migration_module(
            "20260417_0071_parent_planning_type_rename_and_promotion.py",
            "migration_20260417_0071_create_fks",
        )

        bind = MagicMock()
        bind.dialect.name = "postgresql"

        class FakeInspector:
            def get_table_names(self):
                return [
                    "project_install_requests",
                    "workflow_executions",
                    "workflow_checkpoints",
                    "runs",
                    "run_human_input_requests",
                    "workflow_operations",
                ]

            def get_columns(self, table_name):
                columns = {
                    "project_install_requests": [{"name": "workflow_id"}],
                    "workflow_executions": [{"name": "source_workflow_id"}],
                    "workflow_checkpoints": [{"name": "workflow_id"}],
                    "runs": [{"name": "workflow_id"}],
                    "run_human_input_requests": [{"name": "workflow_id"}],
                    "workflow_operations": [{"name": "workflow_id"}],
                }
                return columns[table_name]

            def get_foreign_keys(self, table_name):
                if table_name == "workflow_operations":
                    return [
                        {
                            "name": "workflow_operations_workflow_id_fkey",
                            "referred_table": "workflow_executions",
                            "referred_columns": ["workflow_id"],
                            "constrained_columns": ["workflow_id"],
                        }
                    ]
                return []

        op_mock = MagicMock()
        op_mock.get_bind.return_value = bind
        with patch.object(module, "op", op_mock), patch.object(module.sa, "inspect", return_value=FakeInspector()):
            module._create_workflow_id_foreign_keys(bind)

        created = [call.args[:3] for call in op_mock.create_foreign_key.call_args_list]
        self.assertEqual(
            created,
            [
                ("project_install_requests_workflow_id_fkey", "project_install_requests", "workflow_executions"),
                ("workflow_executions_source_workflow_id_fkey", "workflow_executions", "workflow_executions"),
                ("workflow_checkpoints_workflow_id_fkey", "workflow_checkpoints", "workflow_executions"),
                ("runs_workflow_id_fkey", "runs", "workflow_executions"),
                ("run_human_input_requests_workflow_id_fkey", "run_human_input_requests", "workflow_executions"),
            ],
        )

    def test_jira_feature_migrations_chain_after_staging_worker_head(self) -> None:
        """Branch-specific migrations chained after staging merge head (20260328_0045)."""
        versions_dir = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
        )
        expected_chain = {
            "20260328_0046_platform_settings.py": (
                'revision = "20260328_0046"',
                'down_revision = "20260328_0045"',
            ),
            "20260328_0047_followup_context_identity.py": (
                'revision = "20260328_0047"',
                'down_revision = "20260328_0046"',
            ),
            "20260328_0048_pm_interview_cases.py": (
                'revision = "20260328_0048"',
                'down_revision = "20260328_0047"',
            ),
            "20260328_0049_project_voice_automations.py": (
                'revision = "20260328_0049"',
                'down_revision = "20260328_0048"',
            ),
            "20260330_0050_project_automation_optional_delivery_channel.py": (
                'revision = "20260330_0050"',
                'down_revision = "20260328_0049"',
            ),
            "20260330_0051_drop_project_automation_voice_id.py": (
                'revision = "20260330_0051"',
                'down_revision = "20260330_0050"',
            ),
            "20260330_0052_drop_project_automation_delivery_channel.py": (
                'revision = "20260330_0052"',
                'down_revision = "20260330_0051"',
            ),
            "20260407_0053_workflow_execution.py": (
                'revision = "20260407_0053"',
                'down_revision = "20260330_0052"',
            ),
            "20260407_0054_move_project_secret_refs_to_managed.py": (
                'revision = "20260407_0054"',
                'down_revision = "20260407_0053"',
            ),
            "20260409_0055_project_installs.py": (
                'revision = "20260409_0055"',
                'down_revision = "20260407_0054"',
            ),
            "20260410_0056_execution_snapshot_legacy_payload_backfill.py": (
                'revision = "20260410_0056"',
                'down_revision = "20260409_0055"',
            ),
            "20260410_0057_run_plan_snapshot_followup_backfill.py": (
                'revision = "20260410_0057"',
                'down_revision = "20260410_0056"',
            ),
            "20260410_0058_execution_snapshot_checkpoint_backfill.py": (
                'revision = "20260410_0058"',
                'down_revision = "20260410_0057"',
            ),
            "20260412_0059_run_queue_contract.py": (
                'revision = "20260412_0059"',
                'down_revision = "20260410_0058"',
            ),
            "20260412_0060_run_claim_id.py": (
                'revision = "20260412_0060"',
                'down_revision = "20260412_0059"',
            ),
            "20260412_0061_runtime_readiness.py": (
                'revision = "20260412_0061"',
                'down_revision = "20260412_0060"',
            ),
            "20260412_0062_worker_runtime_kinds.py": (
                'revision = "20260412_0062"',
                'down_revision = "20260412_0061"',
            ),
            "20260413_0063_worker_runtime_auth_requests.py": (
                'revision = "20260413_0063"',
                'down_revision = "20260412_0062"',
            ),
            "20260413_0064_merge_project_installs_and_worker_runtime_auth_heads.py": (
                'revision = "20260413_0064"',
                'down_revision = ("20260410_0058", "20260413_0063")',
            ),
        }

        for filename, expected_lines in expected_chain.items():
            contents = (versions_dir / filename).read_text(encoding="utf-8")
            for expected_line in expected_lines:
                self.assertIn(expected_line, contents)

    def test_run_migrations_repairs_stamp_when_schema_0045_but_version_0044(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            self._alembic_upgrade(database_url, "20260328_0045")
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num = '20260328_0044'"))

            run_migrations(database_url=database_url)

            with engine.begin() as connection:
                versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            self.assertEqual(versions, ["20260424_0095"])

    def test_run_migrations_disables_alembic_logger_reconfiguration(self) -> None:
        fake_config = MagicMock()
        fake_config.attributes = {}
        with (
            patch("orchestrator.storage.migrations._normalize_repaired_top_revisions") as normalize_mock,
            patch("orchestrator.storage.migrations._repair_stamp_if_schema_ahead_of_version") as repair_mock,
            patch("orchestrator.storage.migrations.Config", return_value=fake_config),
            patch("orchestrator.storage.migrations.command.upgrade") as upgrade_mock,
        ):
            run_migrations(database_url="sqlite:///tmp/test.db")

        normalize_mock.assert_called_once_with("sqlite:///tmp/test.db")
        repair_mock.assert_called_once_with("sqlite:///tmp/test.db")
        self.assertEqual(fake_config.attributes.get("configure_logger"), False)
        upgrade_mock.assert_called_once_with(fake_config, "head")

    def test_migrations_create_expected_tables(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"

            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            inspector = inspect(engine)

            self.assertIn("tenants", inspector.get_table_names())
            self.assertIn("atlassian_oauth_connections", inspector.get_table_names())
            self.assertIn("runs", inspector.get_table_names())
            self.assertIn("workflow_executions", inspector.get_table_names())
            self.assertIn("workflow_checkpoints", inspector.get_table_names())
            self.assertIn("webhook_deliveries", inspector.get_table_names())
            self.assertIn("repo_bootstrap_states", inspector.get_table_names())
            self.assertIn("managed_secrets", inspector.get_table_names())
            self.assertIn("agent_lifecycle_events", inspector.get_table_names())
            self.assertIn("knowledge_jira_sync_runtime_states", inspector.get_table_names())
            self.assertIn("knowledge_jira_sync_project_states", inspector.get_table_names())
            self.assertIn("discord_command_sync_runtime_states", inspector.get_table_names())
            self.assertIn("project_automations", inspector.get_table_names())
            self.assertIn("project_automation_executions", inspector.get_table_names())
            self.assertNotIn("run_locks", inspector.get_table_names())

            run_columns = {column["name"] for column in inspector.get_columns("runs")}
            self.assertIn("workflow_id", run_columns)
            self.assertIn("attempt_number", run_columns)
            self.assertIn("parent_run_id", run_columns)
            self.assertIn("entry_mode", run_columns)
            self.assertIn("entry_stage", run_columns)
            self.assertIn("entry_checkpoint_id", run_columns)
            self.assertNotIn("dev_session_id", run_columns)
            self.assertNotIn("pm_session_id", run_columns)
            self.assertNotIn("orchestrated_session_id", run_columns)

            workflow_columns = {column["name"] for column in inspector.get_columns("workflow_executions")}
            self.assertIn("workflow_id", workflow_columns)
            self.assertIn("last_error", workflow_columns)
            self.assertIn("active_run_id", workflow_columns)
            self.assertIn("latest_checkpoint_id", workflow_columns)
            self.assertIn("source_workflow_id", workflow_columns)
            self.assertIn("source_run_id", workflow_columns)
            self.assertIn("updated_at", workflow_columns)

            checkpoint_columns = {column["name"] for column in inspector.get_columns("workflow_checkpoints")}
            self.assertIn("workflow_id", checkpoint_columns)
            self.assertIn("checkpoint_kind", checkpoint_columns)
            self.assertIn("stage", checkpoint_columns)
            self.assertIn("payload_json", checkpoint_columns)
            self.assertIn("codex_session_id", checkpoint_columns)

            human_input_columns = {column["name"] for column in inspector.get_columns("run_human_input_requests")}
            self.assertIn("workflow_id", human_input_columns)
            self.assertIn("checkpoint_id", human_input_columns)
            self.assertIn("consumed_by_run_id", human_input_columns)
            self.assertNotIn("resumed_run_id", human_input_columns)
            self.assertNotIn("resume_stage", human_input_columns)
            self.assertNotIn("resume_session_id", human_input_columns)

            automation_indexes = {index["name"] for index in inspector.get_indexes("project_automations")}
            execution_indexes = {index["name"] for index in inspector.get_indexes("project_automation_executions")}

            self.assertIn("ix_project_automations_due_scan", automation_indexes)
            self.assertIn("ix_project_automation_executions_due_scan", execution_indexes)
            self.assertIn("ix_project_automation_executions_automation_history", execution_indexes)
            automation_columns = {column["name"]: column for column in inspector.get_columns("project_automations")}
            self.assertNotIn("delivery_text_channel_id", automation_columns)
            self.assertIn("worker_runtime_states", inspector.get_table_names())
            knowledge_fact_columns = {column["name"]: column for column in inspector.get_columns("knowledge_facts")}
            self.assertEqual(getattr(knowledge_fact_columns["slot_name"]["type"], "length", None), 128)

    def test_initial_migration_uses_boolean_default_for_tenants_enabled(self) -> None:
        migration_file = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260206_0001_initial.py"
        )
        contents = migration_file.read_text(encoding="utf-8")

        self.assertIn(
            'sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true())',
            contents,
        )
        self.assertNotIn('server_default=sa.text("1")', contents)

    def test_followup_context_identity_migration_adds_explicit_routing_columns(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            inspector = inspect(engine)

            followup_columns = {column["name"] for column in inspector.get_columns("followup_contexts")}
            followup_indexes = {index["name"] for index in inspector.get_indexes("followup_contexts")}

            self.assertIn("owner_user_id", followup_columns)
            self.assertIn("origin_command", followup_columns)
            self.assertIn("ix_followup_contexts_tenant_status_owner", followup_indexes)

    def test_tenant_identity_migration_uses_boolean_defaults_for_postgres(self) -> None:
        migration_file = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260327_0039_tenant_identity_and_invites.py"
        )
        contents = migration_file.read_text(encoding="utf-8")

        self.assertIn(
            'sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true())',
            contents,
        )
        self.assertIn(
            'sa.Column("must_change_password", sa.Boolean(), nullable=False, server_default=sa.false())',
            contents,
        )
        self.assertNotIn('server_default=sa.text("1")', contents)
        self.assertNotIn('server_default=sa.text("0")', contents)

    def test_worker_runtime_state_migration_handles_postgres_duplicate_table_errors(self) -> None:
        migration_file = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260328_0045_worker_runtime_and_knowledge_fact_slot_name.py"
        )
        contents = migration_file.read_text(encoding="utf-8")

        self.assertIn("ProgrammingError", contents)
        self.assertIn('sqlstate == "42P07"', contents)
        self.assertIn("except (IntegrityError, ProgrammingError) as exc:", contents)
        self.assertIn("_is_duplicate_table_error(exc)", contents)

    def test_decision_state_migration_is_idempotent_when_tables_already_exist(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num = '20260309_0021'"))

            run_migrations(database_url=database_url)

            inspector = inspect(engine)
            self.assertIn("decision_cases", inspector.get_table_names())
            self.assertIn("decision_cycles", inspector.get_table_names())
            self.assertIn("decision_events", inspector.get_table_names())
            self.assertIn("decision_effects_outbox", inspector.get_table_names())

    def test_pgvector_migration_serializes_extension_creation(self) -> None:
        migration_file = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260311_0025_knowledge_pgvector_and_search.py"
        )
        contents = migration_file.read_text(encoding="utf-8")

        self.assertIn("pg_advisory_xact_lock", contents)
        self.assertIn("CREATE EXTENSION IF NOT EXISTS vector", contents)

    def test_live_voice_room_links_migration_rewrites_legacy_discord_config(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            run_migrations(database_url=database_url)

            engine = create_engine(database_url)
            now = datetime.now(timezone.utc).isoformat()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO tenants (
                            tenant_id, name, is_enabled, jira_config, github_config, repos_config,
                            policy_config, discord_config, created_at, updated_at
                        ) VALUES (
                            :tenant_id, :name, :is_enabled, :jira_config, :github_config, :repos_config,
                            :policy_config, :discord_config, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "tenant_id": "tenant-a",
                        "name": "Tenant A",
                        "is_enabled": True,
                        "jira_config": json.dumps({}),
                        "github_config": json.dumps({}),
                        "repos_config": json.dumps({}),
                        "policy_config": json.dumps({}),
                        "discord_config": json.dumps(
                            {
                                "live_voice_channel_id": "voice-room-1",
                                "live_voice_linked_text_channel_id": "text-room-1",
                            }
                        ),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO projects (
                            project_id, tenant_id, name, github_repository, jira_project_key,
                            policy_overrides, environment, secret_refs, discord_config, is_archived,
                            created_at, updated_at
                        ) VALUES (
                            :project_id, :tenant_id, :name, :github_repository, :jira_project_key,
                            :policy_overrides, :environment, :secret_refs, :discord_config, :is_archived,
                            :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "project_id": "project-a",
                        "tenant_id": "tenant-a",
                        "name": "Project A",
                        "github_repository": "https://github.com/example/repo",
                        "jira_project_key": "MAB",
                        "policy_overrides": json.dumps({}),
                        "environment": json.dumps({}),
                        "secret_refs": json.dumps({}),
                        "discord_config": json.dumps(
                            {
                                "live_voice_channel_id": "voice-room-1",
                                "live_voice_linked_text_channel_id": "text-room-1",
                                "live_voice_room_links": {
                                    "voice-room-1": "text-room-1",
                                    "voice-room-2": "text-room-2",
                                },
                            }
                        ),
                        "is_archived": False,
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                connection.execute(text("UPDATE alembic_version SET version_num = '20260322_0033'"))

            run_migrations(database_url=database_url)

            with engine.begin() as connection:
                tenant_config = json.loads(
                    connection.execute(
                        text("SELECT discord_config FROM tenants WHERE tenant_id = 'tenant-a'")
                    ).scalar_one()
                )
                project_config = json.loads(
                    connection.execute(
                        text("SELECT discord_config FROM projects WHERE project_id = 'project-a'")
                    ).scalar_one()
                )

            self.assertEqual(
                tenant_config,
                {
                    "live_voice_room_links": {
                        "voice-room-1": "text-room-1",
                    }
                },
            )
            self.assertEqual(
                project_config,
                {
                    "live_voice_room_links": {
                        "voice-room-1": "text-room-1",
                        "voice-room-2": "text-room-2",
                    }
                },
            )

    def test_postgres_event_transport_migration_drops_old_tables(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            self._alembic_upgrade(database_url, "20260424_0093")
            engine = create_engine(database_url)
            before = set(inspect(engine).get_table_names())
            self.assertIn("run_log_events", before)
            self.assertIn("run_stream_events", before)
            self.assertIn("audit_events", before)
            self.assertIn("observability_stream_events", before)

            self._alembic_upgrade(database_url, "20260424_0095")

            inspector = inspect(engine)
            self.assertNotIn("run_log_events", inspector.get_table_names())
            self.assertNotIn("run_stream_events", inspector.get_table_names())
            self.assertNotIn("audit_events", inspector.get_table_names())
            self.assertNotIn("observability_stream_events", inspector.get_table_names())

    def test_run_migrations_rejects_sqlite_without_test_opt_in(self) -> None:
        previous = os.environ.get("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS")
        try:
            os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = "false"
            get_settings.cache_clear()
            with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
                run_migrations(database_url="sqlite:///tmp/test.db")
        finally:
            if previous is None:
                os.environ.pop("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS", None)
            else:
                os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = previous
            get_settings.cache_clear()

    def test_workflow_execution_migration_backfills_existing_runs_and_scrubs_resume_metadata(self) -> None:
        with TemporaryDirectory() as tmp_dir:
            database_url = f"sqlite:///{tmp_dir}/test.db"
            self._alembic_upgrade(database_url, "20260330_0052")

            engine = create_engine(database_url)
            now = datetime.now(timezone.utc).isoformat()
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO tenants (
                            tenant_id, name, is_enabled, jira_config, github_config, repos_config,
                            policy_config, discord_config, created_at, updated_at
                        ) VALUES (
                            :tenant_id, :name, :is_enabled, :jira_config, :github_config, :repos_config,
                            :policy_config, :discord_config, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "tenant_id": "tenant-a",
                        "name": "Tenant A",
                        "is_enabled": True,
                        "jira_config": json.dumps({}),
                        "github_config": json.dumps({}),
                        "repos_config": json.dumps({}),
                        "policy_config": json.dumps({}),
                        "discord_config": json.dumps({}),
                        "created_at": now,
                        "updated_at": now,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO runs (
                            run_id, tenant_id, issue_key, issue_summary, issue_description, repo_url, branch, pr_url,
                            dev_session_id, pm_session_id, orchestrated_session_id, dedupe_scope, status, last_error,
                            plan, created_at, started_at, finished_at
                        ) VALUES (
                            :run_id, :tenant_id, :issue_key, :issue_summary, :issue_description, :repo_url, :branch, :pr_url,
                            :dev_session_id, :pm_session_id, :orchestrated_session_id, :dedupe_scope, :status, :last_error,
                            :plan, :created_at, :started_at, :finished_at
                        )
                        """
                    ),
                    {
                        "run_id": "run-legacy-1",
                        "tenant_id": "tenant-a",
                        "issue_key": "GP-184",
                        "issue_summary": "Legacy run",
                        "issue_description": "Legacy run description",
                        "repo_url": "https://github.com/example/repo",
                        "branch": "feature/GP-184",
                        "pr_url": None,
                        "dev_session_id": "dev-session-1",
                        "pm_session_id": None,
                        "orchestrated_session_id": None,
                        "dedupe_scope": "issue_execution",
                        "status": "failed",
                        "last_error": "boom",
                        "plan": json.dumps(
                            {
                                "trigger_context": {
                                    "resume_stage": "dev",
                                    "resume_session_id": "dev-session-1",
                                    "resume_source_run_id": "run-legacy-0",
                                    "resume_source_plan": {"plan_steps": ["restore auth flow"]},
                                    "resume_source_state": {"review_feedback": "check it"},
                                    "human_input_request_ids": ["request-1"],
                                    "source": "manual",
                                },
                                "plan": {"plan_steps": ["restore auth flow"]},
                            }
                        ),
                        "created_at": now,
                        "started_at": now,
                        "finished_at": now,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO run_human_input_requests (
                            request_id, tenant_id, project_id, source_run_id, resumed_run_id, issue_key, source_stage,
                            resume_stage, resume_session_id, request_type, prompt, instructions,
                            expected_reply_format, status, request_context_json, thread_channel_id, thread_message_id,
                            answer_encrypted, answer_source_ref, answered_at, expires_at, created_at, updated_at
                        ) VALUES (
                            :request_id, :tenant_id, :project_id, :source_run_id, :resumed_run_id, :issue_key, :source_stage,
                            :resume_stage, :resume_session_id, :request_type, :prompt, :instructions,
                            :expected_reply_format, :status, :request_context_json, :thread_channel_id, :thread_message_id,
                            :answer_encrypted, :answer_source_ref, :answered_at, :expires_at, :created_at, :updated_at
                        )
                        """
                    ),
                    {
                        "request_id": "request-1",
                        "tenant_id": "tenant-a",
                        "project_id": None,
                        "source_run_id": "run-legacy-1",
                        "resumed_run_id": None,
                        "issue_key": "GP-184",
                        "source_stage": "dev",
                        "resume_stage": "dev",
                        "resume_session_id": "dev-session-1",
                        "request_type": "verification_code",
                        "prompt": "Reply with the code",
                        "instructions": None,
                        "expected_reply_format": None,
                        "status": "pending",
                        "request_context_json": json.dumps(
                            {
                                "resume_source_plan": {"plan_steps": ["restore auth flow"]},
                                "resume_source_state": {"review_feedback": "check it"},
                                "human_input_request_ids": ["request-1"],
                            }
                        ),
                        "thread_channel_id": "thread-1",
                        "thread_message_id": "message-1",
                        "answer_encrypted": None,
                        "answer_source_ref": None,
                        "answered_at": None,
                        "expires_at": now,
                        "created_at": now,
                        "updated_at": now,
                    },
                )

            run_migrations(database_url=database_url)

            with engine.begin() as connection:
                workflow_row = connection.execute(
                    text(
                        """
                        SELECT workflow_id, status, active_run_id, latest_checkpoint_id
                        FROM workflow_executions
                        WHERE source_ref = 'GP-184'
                        """
                    )
                ).mappings().one()
                run_row = connection.execute(
                    text(
                        """
                        SELECT workflow_id, attempt_number, parent_run_id, entry_mode, entry_stage, entry_checkpoint_id, plan
                        FROM runs
                        WHERE run_id = 'run-legacy-1'
                        """
                    )
                ).mappings().one()
                request_row = connection.execute(
                    text(
                        """
                        SELECT workflow_id, checkpoint_id, consumed_by_run_id, status, request_context_json
                        FROM run_human_input_requests
                        WHERE request_id = 'request-1'
                        """
                    )
                ).mappings().one()
                checkpoint_row = connection.execute(
                    text(
                        """
                        SELECT checkpoint_id, workflow_id, checkpoint_kind, stage, payload_json, codex_session_id
                        FROM workflow_checkpoints
                        WHERE run_id = 'run-legacy-1'
                        """
                    )
                ).mappings().one()

            self.assertEqual(workflow_row["workflow_id"], run_row["workflow_id"])
            self.assertIsNone(workflow_row["active_run_id"])
            self.assertEqual(workflow_row["latest_checkpoint_id"], run_row["entry_checkpoint_id"])
            self.assertEqual(run_row["attempt_number"], 1)
            self.assertIsNone(run_row["parent_run_id"])
            self.assertEqual(run_row["entry_mode"], "fresh")
            self.assertEqual(run_row["entry_stage"], "orchestrated")
            self.assertEqual(run_row["entry_checkpoint_id"], checkpoint_row["checkpoint_id"])
            self.assertEqual(request_row["workflow_id"], workflow_row["workflow_id"])
            self.assertEqual(request_row["checkpoint_id"], checkpoint_row["checkpoint_id"])
            self.assertIsNone(request_row["consumed_by_run_id"])
            self.assertEqual(request_row["status"], "pending")
            self.assertEqual(checkpoint_row["workflow_id"], workflow_row["workflow_id"])
            self.assertEqual(checkpoint_row["checkpoint_kind"], "orchestrated")
            self.assertEqual(checkpoint_row["stage"], "orchestrated")
            self.assertIsNone(checkpoint_row["codex_session_id"])

            run_plan = json.loads(run_row["plan"]) if isinstance(run_row["plan"], str) else run_row["plan"]
            request_context = (
                json.loads(request_row["request_context_json"])
                if isinstance(request_row["request_context_json"], str)
                else request_row["request_context_json"]
            )
            checkpoint_payload = (
                json.loads(checkpoint_row["payload_json"])
                if isinstance(checkpoint_row["payload_json"], str)
                else checkpoint_row["payload_json"]
            )
            self.assertEqual(run_plan["context"]["trigger_context"], {"source": "manual"})
            self.assertEqual(request_context, {})
            self.assertEqual(checkpoint_payload.get("context", {}).get("trigger_context"), {"source": "manual"})

    def test_project_secret_migration_moves_existing_refs_to_project_managed_secrets(self) -> None:
        previous_key = os.environ.get("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY")
        encryption_key = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
        try:
            os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = encryption_key
            get_settings.cache_clear()
            with TemporaryDirectory() as tmp_dir:
                database_url = f"sqlite:///{tmp_dir}/test.db"
                self._alembic_upgrade(database_url, "20260407_0053")

                engine = create_engine(database_url)
                now = datetime.now(timezone.utc).isoformat()
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            """
                            INSERT INTO tenants (
                                tenant_id, name, is_enabled, jira_config, github_config, repos_config,
                                policy_config, discord_config, created_at, updated_at
                            ) VALUES (
                                :tenant_id, :name, :is_enabled, :jira_config, :github_config, :repos_config,
                                :policy_config, :discord_config, :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            "tenant_id": "tenant-a",
                            "name": "Tenant A",
                            "is_enabled": True,
                            "jira_config": json.dumps({}),
                            "github_config": json.dumps({}),
                            "repos_config": json.dumps({}),
                            "policy_config": json.dumps({}),
                            "discord_config": json.dumps({}),
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO projects (
                                project_id, tenant_id, name, github_repository, jira_project_key,
                                policy_overrides, environment, secret_refs, discord_config, is_archived,
                                created_at, updated_at
                            ) VALUES (
                                :project_id, :tenant_id, :name, :github_repository, :jira_project_key,
                                :policy_overrides, :environment, :secret_refs, :discord_config, :is_archived,
                                :created_at, :updated_at
                            )
                            """
                        ),
                        {
                            "project_id": "project-1",
                            "tenant_id": "tenant-a",
                            "name": "Project One",
                            "github_repository": "https://github.com/example/project-one",
                            "jira_project_key": "APP",
                            "policy_overrides": json.dumps({}),
                            "environment": json.dumps({}),
                            "secret_refs": json.dumps(
                                {
                                    "SERVICE_ID": "SUPABASE_APPLE_SERVICE_ID",
                                    "CALLBACK_URL": "tenant/tenant-a/SUPABASE_APPLE_CALLBACK_URL",
                                    "INLINE_SECRET": "Ft6ygA&aYkf%hy",
                                }
                            ),
                            "discord_config": json.dumps({}),
                            "is_archived": False,
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO managed_secrets (secret_ref, value_encrypted, created_at, updated_at)
                            VALUES (:secret_ref, :value_encrypted, :created_at, :updated_at)
                            """
                        ),
                        {
                            "secret_ref": "tenant/tenant-a/SUPABASE_APPLE_SERVICE_ID",
                            "value_encrypted": encrypt_value(
                                plaintext="com.route25.app",
                                encryption_key=encryption_key,
                            ),
                            "created_at": now,
                            "updated_at": now,
                        },
                    )
                    connection.execute(
                        text(
                            """
                            INSERT INTO managed_secrets (secret_ref, value_encrypted, created_at, updated_at)
                            VALUES (:secret_ref, :value_encrypted, :created_at, :updated_at)
                            """
                        ),
                        {
                            "secret_ref": "tenant/tenant-a/SUPABASE_APPLE_CALLBACK_URL",
                            "value_encrypted": encrypt_value(
                                plaintext="https://route25.example.com/auth/callback",
                                encryption_key=encryption_key,
                            ),
                            "created_at": now,
                            "updated_at": now,
                        },
                    )

                run_migrations(database_url=database_url)

                with engine.begin() as connection:
                    project_row = connection.execute(
                        text(
                            """
                            SELECT secret_refs
                            FROM projects
                            WHERE project_id = 'project-1'
                            """
                        )
                    ).mappings().one()
                    managed_rows = connection.execute(
                        text(
                            """
                            SELECT secret_ref
                            FROM managed_secrets
                            WHERE secret_ref IN (
                                'project/tenant-a/project-1/SERVICE_ID',
                                'project/tenant-a/project-1/CALLBACK_URL',
                                'project/tenant-a/project-1/INLINE_SECRET'
                            )
                            ORDER BY secret_ref
                            """
                        )
                    ).scalars().all()

                self.assertEqual(
                    json.loads(project_row["secret_refs"]),
                    {
                        "CALLBACK_URL": "project/tenant-a/project-1/CALLBACK_URL",
                        "INLINE_SECRET": "project/tenant-a/project-1/INLINE_SECRET",
                        "SERVICE_ID": "project/tenant-a/project-1/SERVICE_ID",
                    },
                )
                self.assertEqual(
                    managed_rows,
                    [
                        "project/tenant-a/project-1/CALLBACK_URL",
                        "project/tenant-a/project-1/INLINE_SECRET",
                        "project/tenant-a/project-1/SERVICE_ID",
                    ],
                )
        finally:
            if previous_key is None:
                os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
            else:
                os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = previous_key
            get_settings.cache_clear()
