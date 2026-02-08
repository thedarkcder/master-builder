import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Run
from orchestrator.tools.jira_oauth import JiraIssuePreview


class DiscordCommandApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/discord_commands_test.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.client = TestClient(create_app())
        self.session_factory = create_session_factory(database_url=self.database_url)

        create_response = self.client.post(
            "/api/admin/tenants",
            json={
                "name": "Discord Tenant",
                "is_enabled": True,
                "jira": {
                    "connection_id": None,
                    "project_keys": ["TP"],
                    "ready_statuses": ["To Do"],
                    "ready_jql": 'project = TP AND status = "To Do"',
                    "ready_label": "agent:ready",
                    "in_progress_label": "agent:in-progress",
                    "blocked_label": "agent:blocked",
                    "done_label": None,
                    "webhook_secret_ref": None,
                },
                "github": {
                    "mode": "github_app",
                    "webhook_secret_ref": None,
                    "installation_id": "12345",
                },
                "repos": {"github_repository": "https://github.com/example/repo"},
                "policy": {
                    "allow_jira_transitions": False,
                    "allow_pr_creation": True,
                    "allow_label_mutations": True,
                    "max_runtime_minutes": 30,
                    "max_dev_test_review_loops": 2,
                    "max_concurrent_runs": 2,
                    "allowed_commands": [],
                    "require_agents_md": False,
                },
                "discord": {
                    "channel_id": "discord-channel-1",
                    "channel_name_template": "proj-{tenant_id}",
                    "notify_events": ["run_started"],
                    "allowed_user_ids": ["u-admin"],
                    "command_secret_ref": None,
                },
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        self.tenant_id = create_response.json()["tenant_id"]

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _queue_run(self, *, run_id: str, issue_key: str, status: str) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Run(
                    run_id=run_id,
                    tenant_id=self.tenant_id,
                    issue_key=issue_key,
                    issue_summary=f"Issue {issue_key}",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    status=status,
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=now if status == "running" else None,
                    finished_at=None if status in {"queued", "running"} else now,
                )
            )
            session.commit()

    def test_help_command_returns_supported_commands(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("!run", response.json()["message"])

    def test_status_command_includes_queue_and_active_runs(self) -> None:
        self._queue_run(run_id="run-queued-1", issue_key="TP-10", status="queued")
        self._queue_run(run_id="run-running-1", issue_key="TP-11", status="running")

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!status"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["queue_depth"], 1)
        self.assertEqual(len(response.json()["data"]["active_runs"]), 1)

    def test_sensitive_commands_require_allowlisted_user(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!run TP-99"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("allowlisted", response.json()["detail"])

    def test_cancel_marks_run_as_cancelled(self) -> None:
        self._queue_run(run_id="run-cancel-me", issue_key="TP-12", status="queued")

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!cancel run-cancel-me"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "cancelled")

    def test_run_enqueues_when_jira_issue_is_executable(self) -> None:
        with patch(
            "orchestrator.api.routes_discord._fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-20")

    def test_retry_enqueues_from_latest_failed_run(self) -> None:
        self._queue_run(run_id="run-failed-1", issue_key="TP-30", status="failed")
        with patch(
            "orchestrator.api.routes_discord._fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry TP-30"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-30")
