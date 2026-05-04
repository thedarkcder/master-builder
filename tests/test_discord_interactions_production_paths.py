from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.platform.secret_service import platform_secret_service
from orchestrator.core.runtime.payload_models import AskIntentPayload
from orchestrator.core.platform.secrets import encrypt_value
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import (
    DecisionCase,
    AtlassianOAuthConnection,
    Project,
    Tenant,
)
from tests.production_path_support import load_json_fixture

pytestmark = pytest.mark.production_path


class _FakeDiscordApiClient:
    def __init__(self) -> None:
        self.posted_messages: list[dict[str, object]] = []
        self.created_threads: list[dict[str, str]] = []
        self._next_message_ids = ["123456789012345", "123456789012346", "123456789012347"]

    def post_message(self, *, channel_id: str, content: str, components=None):  # noqa: ANN001
        payload = {
            "channel_id": channel_id,
            "content": content,
            "components": components,
        }
        self.posted_messages.append(payload)
        message_id = self._next_message_ids.pop(0) if self._next_message_ids else f"msg-{len(self.posted_messages)}"
        return {"id": message_id}

    def create_thread_from_message(self, *, channel_id: str, message_id: str, name: str) -> str:
        self.created_threads.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "name": name,
            }
        )
        return "thread-1"

    def ensure_thread_for_message(self, *, channel_id: str, message_id: str, thread_name: str) -> str:
        self.created_threads.append(
            {
                "channel_id": channel_id,
                "message_id": message_id,
                "name": thread_name,
            }
        )
        return "thread-1"

    def get_channel(self, *, channel_id: str) -> dict[str, str]:
        return {"id": channel_id, "name": f"route25-ask-{channel_id[-6:]}"}


class DiscordInteractionsProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/discord_interactions_production.db"
        self.checkout_dir = os.path.join(self.temp_dir.name, "checkouts")

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = self.checkout_dir

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

        self._seed_runtime_state()
        self.private_key = Ed25519PrivateKey.generate()
        public_key_hex = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()
        with self.session_factory() as session:
            platform_secret_service.upsert_secret(
                session=session,
                secret_ref="DISCORD_INTERACTIONS_PUBLIC_KEY",
                plaintext_value=public_key_hex,
                encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
            )
            platform_secret_service.upsert_secret(
                session=session,
                secret_ref="DISCORD_BOT_TOKEN",
                plaintext_value="discord-token",
                encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
            )
            session.commit()

        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        os.environ.pop("ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _seed_runtime_state(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="route25",
                name="Route25",
                is_enabled=True,
                jira_config={
                    "connection_id": "conn-1",
                    "project_keys": ["TP"],
                    "ready_statuses": ["To Do"],
                    "ready_jql": 'project = TP AND status = "To Do"',
                    "ready_label": "agent:ready",
                    "in_progress_label": "agent:in-progress",
                    "blocked_label": "agent:blocked",
                    "done_label": None,
                    "webhook_secret_ref": None,
                },
                github_config={"mode": "github_app", "installation_id": "12345"},
                repos_config={"github_repository": "https://github.com/example/repo"},
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
                discord_config={
                    "channel_id": "discord-channel-1",
                    "notify_events": [],
                    "allowed_user_ids": ["u-1"],
                },
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="route25-default",
                tenant_id="route25",
                name="Route25 Default",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={
                    "channel_id": "discord-channel-1",
                    "notify_events": [],
                    "allowed_user_ids": ["u-1"],
                },
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            connection = AtlassianOAuthConnection(
                connection_id="conn-1",
                account_id="account-1",
                account_email="test@example.com",
                cloud_id="cloud-1",
                site_url="https://example.atlassian.net",
                scopes=["read:jira-work", "write:jira-work"],
                access_token_encrypted=encrypt_value(
                    plaintext="access-token",
                    encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                ),
                refresh_token_encrypted=encrypt_value(
                    plaintext="refresh-token",
                    encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                ),
                access_token_expires_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, connection])
            session.commit()

    def _signed_headers(self, payload_bytes: bytes) -> dict[str, str]:
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        signature = self.private_key.sign(timestamp.encode("utf-8") + payload_bytes).hex()
        return {
            "Content-Type": "application/json",
            "X-Signature-Ed25519": signature,
            "X-Signature-Timestamp": timestamp,
        }

    def _post_interaction(self, payload: dict) -> object:
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        return self.client.post(
            "/discord/interactions",
            content=payload_bytes,
            headers=self._signed_headers(payload_bytes),
        )

    def _process_one_job(self):
        with self.session_factory() as session:
            return process_next_webhook_job(
                session=session,
                settings=get_settings(),
                owner_id="worker:test",
            )

    def test_application_help_command_runs_real_queued_followup_and_creates_thread(self) -> None:
        discord_client = _FakeDiscordApiClient()
        payload = load_json_fixture("discord", "interactions", "application_command_help.json")

        with (
            patch(
                "orchestrator.api.discord.interactions.followup_transport.DiscordApiClient",
                return_value=discord_client,
            ),
        ):
            response = self._post_interaction(payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["type"], 5)
            self._process_one_job()

        with self.session_factory() as session:
            project = session.get(Project, "route25-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            self.assertEqual(discord_config.get("ask_thread_by_message_id", {}).get("123456789012345"), "thread-1")
            self.assertIn("thread-1", discord_config.get("ask_thread_channel_ids", []))

        self.assertEqual(len(discord_client.created_threads), 1)
        self.assertEqual(discord_client.created_threads[0]["channel_id"], "discord-channel-1")
        self.assertEqual(len(discord_client.posted_messages), 2)
        self.assertEqual(discord_client.posted_messages[0]["channel_id"], "discord-channel-1")
        self.assertIn("!help", str(discord_client.posted_messages[0]["content"]))
        self.assertEqual(discord_client.posted_messages[1]["channel_id"], "thread-1")
        self.assertIn("Continue here with follow-up questions", str(discord_client.posted_messages[1]["content"]))

    def test_stale_issue_bound_reply_modal_falls_back_to_ask_followup(self) -> None:
        discord_client = _FakeDiscordApiClient()
        issue_key = "TP-42"
        with self.session_factory() as session:
            project = session.get(Project, "route25-default")
            self.assertIsNotNone(project)
            project.discord_config = {
                **dict(project.discord_config or {}),
                "ask_thread_channel_ids": ["thread-1"],
                "ask_thread_by_message_id": {"123456789012345": "thread-1"},
                "thread_issue_by_channel_id": {"thread-1": issue_key},
            }
            session.add(
                DecisionCase(
                    case_id="case-closed",
                    tenant_id="route25",
                    project_id="route25-default",
                    issue_key=issue_key,
                    state="blocked",
                    blocked_reason="decision_gate_required",
                    classification="decision_gate",
                    issue_fingerprint="fingerprint-1",
                    active_cycle_id=None,
                    last_source="discord_run",
                    last_event_type="discord_run",
                    last_event_at=datetime.now(timezone.utc),
                    required_worker_capability="linux",
                    required_worker_label="worker:linux",
                    ready_label="agent:ready",
                    ready_label_present=True,
                    metadata_json={},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.commit()

        payload = load_json_fixture("discord", "interactions", "modal_submit_ask_reply.json")
        fake_jira_client = type(
            "FakeJiraClient",
            (),
            {
                "get_issue_detail": lambda self, **_: type(
                    "IssueDetail",
                    (),
                    {
                        "key": issue_key,
                        "summary": "Cross-account relink policy",
                        "status": "To Do",
                        "description": "Clarify relink policy.",
                        "labels": [],
                    },
                )(),
            },
        )()

        with (
            patch(
                "orchestrator.api.discord.interactions.followup_transport.DiscordApiClient",
                return_value=discord_client,
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.refresh_atlassian_connection_tokens",
                return_value="access-token",
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.atlassian_oauth_client",
                return_value=fake_jira_client,
            ),
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[],
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="Board answer", command=None),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
                return_value="Board answer",
            ),
        ):
            response = self._post_interaction(payload)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["type"], 5)
            self._process_one_job()

        self.assertEqual(discord_client.created_threads, [])
        self.assertEqual(len(discord_client.posted_messages), 1)
        self.assertEqual(discord_client.posted_messages[0]["channel_id"], "thread-1")
        self.assertIn("Board answer", str(discord_client.posted_messages[0]["content"]))
        self.assertNotIn("No active Decision Gate cycle exists", str(discord_client.posted_messages[0]["content"]))


if __name__ == "__main__":
    unittest.main()
