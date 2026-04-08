from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch
from types import SimpleNamespace

from orchestrator.cli import main as cli_main
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant


class CliEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/cli_test.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._create_tenant("tenant-cli")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _create_tenant(self, tenant_id: str) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Tenant(
                    tenant_id=tenant_id,
                    name="CLI Tenant",
                    is_enabled=True,
                    jira_config={
                        "mcp_endpoint": "https://mcp.example.test",
                        "project_keys": ["TP"],
                        "ready_label": "agent:ready",
                        "in_progress_label": "agent:in-progress",
                        "blocked_label": "agent:blocked",
                        "done_label": "agent:done",
                        "webhook_secret_ref": None,
                    },
                    github_config={
                        "mode": "github_app",
                        "app_id_ref": "secret/app-id",
                        "private_key_ref": "secret/private-key",
                        "webhook_secret_ref": None,
                        "installation_id": "12345",
                    },
                    repos_config={
                        "github_repository": "https://github.com/example/repo",
                    },
                    policy_config={
                        "allow_jira_transitions": False,
                        "allow_pr_creation": True,
                        "allow_label_mutations": True,
                        "max_runtime_minutes": 30,
                        "max_dev_test_review_loops": 2,
                        "max_concurrent_runs": 2,
                        "allowed_commands": [],
                        "require_agents_md": False,
                    },
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_run_command_enqueues_issue(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(["run", "--tenant", "tenant-cli", "--issue", "TP-500"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["enqueued"])
        self.assertEqual(payload["tenant_id"], "tenant-cli")
        self.assertEqual(payload["issue_key"], "TP-500")

    def test_run_command_rejects_unknown_tenant(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(["run", "--tenant", "missing-tenant", "--issue", "TP-501"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("Unknown tenant", payload["error"])

    def test_poll_command_returns_tenant_snapshot(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(["poll", "--tenant", "all"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["tenant_filter"], "all")
        self.assertEqual(len(payload["tenants"]), 1)
        self.assertEqual(payload["tenants"][0]["tenant_id"], "tenant-cli")

    def test_run_command_rejects_sqlite_without_test_opt_in(self) -> None:
        previous = os.environ.get("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS")
        try:
            os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = "false"
            get_settings.cache_clear()
            with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
                cli_main(["run", "--tenant", "tenant-cli", "--issue", "TP-502"])
        finally:
            if previous is None:
                os.environ.pop("ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS", None)
            else:
                os.environ["ORCHESTRATOR_ALLOW_SQLITE_FOR_TESTS"] = previous
            get_settings.cache_clear()

    def test_discord_gateway_command_invokes_runtime(self) -> None:
        with patch("orchestrator.cli.run_discord_gateway") as gateway_mock:
            exit_code = cli_main(["discord-gateway"])
        self.assertEqual(exit_code, 0)
        gateway_mock.assert_called_once_with()

    def test_discord_live_voice_command_invokes_runtime(self) -> None:
        with patch("orchestrator.cli.run_discord_live_voice") as voice_mock:
            exit_code = cli_main(["discord-live-voice"])
        self.assertEqual(exit_code, 0)
        voice_mock.assert_called_once_with()

    def test_knowledge_jira_sync_command_invokes_runtime(self) -> None:
        with patch("orchestrator.cli.run_knowledge_jira_sync") as sync_mock:
            exit_code = cli_main(["knowledge-jira-sync"])
        self.assertEqual(exit_code, 0)
        sync_mock.assert_called_once_with()

    def test_knowledge_prewarm_command_invokes_prewarm(self) -> None:
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("orchestrator.cli.prewarm_knowledge_dependencies") as prewarm_mock,
        ):
            prewarm_mock.return_value.embedding_model = "BAAI/bge-small-en-v1.5"
            exit_code = cli_main(["knowledge-prewarm"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["embedding_model"], "BAAI/bge-small-en-v1.5")
        prewarm_mock.assert_called_once()

    def test_voice_prewarm_command_invokes_prewarm(self) -> None:
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("orchestrator.cli.prewarm_voice_dependencies") as prewarm_mock,
        ):
            prewarm_mock.return_value.voice_stt_provider = "openai"
            prewarm_mock.return_value.voice_tts_provider = "pocket_tts"
            prewarm_mock.return_value.transcription_ready = True
            prewarm_mock.return_value.prewarmed_voice_ids = ("alba", "jean")
            exit_code = cli_main(["voice-prewarm"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["voice_stt_provider"], "openai")
        self.assertEqual(payload["voice_tts_provider"], "pocket_tts")
        self.assertTrue(payload["transcription_ready"])
        self.assertEqual(payload["prewarmed_voice_ids"], ["alba", "jean"])
        prewarm_mock.assert_called_once()

    def test_migrate_execution_snapshots_command_invokes_migration(self) -> None:
        output = io.StringIO()
        migration_report = SimpleNamespace(
            scanned_runs=3,
            converted_runs=2,
            invalid_runs=0,
            scanned_checkpoints=4,
            converted_checkpoints=1,
            invalid_checkpoints=0,
            invalid_run_ids=(),
            invalid_checkpoint_ids=(),
        )
        with (
            redirect_stdout(output),
            patch("orchestrator.cli.migrate_execution_snapshots", return_value=migration_report) as migrate_mock,
        ):
            exit_code = cli_main(["migrate-execution-snapshots", "--apply"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["apply"])
        self.assertEqual(payload["converted_runs"], 2)
        self.assertEqual(payload["converted_checkpoints"], 1)
        migrate_mock.assert_called_once()
