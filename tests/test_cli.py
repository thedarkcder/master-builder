from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_voice_prewarm_command_invokes_prewarm(self) -> None:
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("orchestrator.cli.prewarm_voice_dependencies") as prewarm_mock,
        ):
            prewarm_mock.return_value.transcription_provider = "whisper"
            prewarm_mock.return_value.transcription_ready = True
            prewarm_mock.return_value.voice_reply_provider = "pocket_tts"
            prewarm_mock.return_value.prewarmed_voice_ids = ("alba", "jean")
            exit_code = cli_main(["voice-prewarm"])

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["transcription_provider"], "whisper")
        self.assertTrue(payload["transcription_ready"])
        self.assertEqual(payload["voice_reply_provider"], "pocket_tts")
        self.assertEqual(payload["prewarmed_voice_ids"], ["alba", "jean"])
        prewarm_mock.assert_called_once()
