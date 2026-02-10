import asyncio
import os
import json
import hmac
import hashlib
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from orchestrator.api.main import create_app
from orchestrator.api.routes_webhook import (
    _build_command_followup_message,
    _discord_issue_autocomplete_choices,
    _find_tenant_for_discord_channel,
    _parse_jira_comment_command,
    _parse_discord_interaction_command,
    _send_discord_thread_followup,
    _run_discord_command_followup,
)
from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.discord_channel_tenant_index import invalidate_discord_channel_tenant_index
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Run, Tenant
from orchestrator.tools.discord_api import DiscordApiError
from orchestrator.tools.jira_oauth import JiraIssuePreview


class JiraWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/webhook_test.db"
        self.webhook_secret_env = "ORCHESTRATOR_TEST_WEBHOOK_SECRET"
        self.webhook_secret_value = "super-secret-token"
        self.github_webhook_secret_env = "ORCHESTRATOR_TEST_GITHUB_WEBHOOK_SECRET"
        self.github_webhook_secret_value = "github-super-secret-token"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ[self.webhook_secret_env] = self.webhook_secret_value
        os.environ[self.github_webhook_secret_env] = self.github_webhook_secret_value

        get_settings.cache_clear()
        reset_db_engine_cache()
        invalidate_discord_channel_tenant_index()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

        self.client = TestClient(create_app())
        self._create_tenant("tenant-webhook")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop(self.webhook_secret_env, None)
        os.environ.pop(self.github_webhook_secret_env, None)
        os.environ.pop("ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF", None)
        os.environ.pop("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()
        invalidate_discord_channel_tenant_index()

    def _create_tenant(
        self,
        tenant_id: str,
        webhook_secret_ref: str | None = None,
        github_webhook_secret_ref: str | None = None,
        github_installation_id: str = "12345",
        is_enabled: bool = True,
        max_concurrent_runs: int = 2,
    ) -> None:
        payload = {
            "name": tenant_id,
            "is_enabled": is_enabled,
            "jira": {
                "mcp_endpoint": "https://mcp.example.test",
                "project_keys": ["TP"],
                "ready_statuses": ["Ready for Agent"],
                "ready_jql": 'project = TP AND status = "Ready for Agent"',
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": None,
                "webhook_secret_ref": webhook_secret_ref,
            },
            "github": {
                "mode": "github_app",
                "webhook_secret_ref": github_webhook_secret_ref,
                "installation_id": github_installation_id,
            },
            "repos": {
                "allowlist": ["https://github.com/example/repo"],
                "mapping_rules_by_project_key": {"TP": "https://github.com/example/repo"},
                "mapping_rules_by_component": {},
                "fallback_repo": None,
            },
            "policy": {
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": max_concurrent_runs,
                "allowed_commands": [],
                "require_agents_md": False,
            },
            "discord": None,
        }
        response = self.client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["tenant_id"], tenant_id)

    def _sign_github_payload(self, payload_bytes: bytes, secret: str) -> str:
        digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def _jira_issue_payload(
        self,
        *,
        issue_key: str,
        labels: list[str] | None = None,
        status_name: str = "Ready for Agent",
        status_category_key: str = "indeterminate",
    ) -> dict:
        return {
            "issue": {
                "key": issue_key,
                "fields": {
                    "labels": labels or [],
                    "status": {
                        "name": status_name,
                        "statusCategory": {"key": status_category_key},
                    },
                },
            }
        }

    def test_webhook_ignores_disabled_tenant(self) -> None:
        self._create_tenant("tenant-disabled", is_enabled=False)
        payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/tenant-disabled", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "tenant_disabled")

    def test_webhook_requires_ready_status(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-123", status_name="To Do", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "status_not_ready")
        self.assertIn("Move the issue to a ready status", response.json()["guidance"])

    def test_webhook_ignores_done_issue_status(self) -> None:
        payload = self._jira_issue_payload(
            issue_key="TP-123",
            status_name="Done",
            status_category_key="done",
            labels=["agent:ready"],
        )

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "issue_done")

    def test_webhook_enqueues_once_for_active_issue(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-124", labels=["agent:ready"])

        first = self.client.post("/jira/webhook/tenant-webhook", json=payload)
        second = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])
        self.assertEqual(first.json()["trigger_reason"], "ready_status_recheck")

        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "run_already_active")
        self.assertEqual(second.json()["run_id"], first.json()["run_id"])
        self.assertEqual(second.json()["trigger_reason"], "ready_status_recheck")

    def test_webhook_records_last_delivery_metadata(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-777", labels=["agent:ready"])
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-meta-1"}

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enqueued"])

        tenant_response = self.client.get("/api/admin/tenants/tenant-webhook", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        jira_config = tenant_response.json()["jira"]
        self.assertEqual(jira_config["webhook_last_delivery_id"], "delivery-meta-1")
        self.assertEqual(jira_config["webhook_last_issue_key"], "TP-777")
        self.assertIsNotNone(jira_config["webhook_last_received_at"])

    def test_webhook_issue_deleted_clears_discord_ask_history(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            discord_config = {
                "channel_id": "discord-channel-1",
                "notify_events": ["run_started"],
                "allowed_user_ids": ["u-admin"],
                "ask_history": [
                    {
                        "user_id": "u-viewer",
                        "channel_id": "discord-channel-1",
                        "question": "What changed?",
                        "answer": "Previous answer",
                        "issue_key": "TP-404",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
            tenant.discord_config = discord_config
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-404", labels=["agent:ready"])
        payload["webhookEvent"] = "jira:issue_deleted"

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "issue_deleted")
        self.assertEqual(response.json()["removed_history_entries"], 1)

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            ask_history = (tenant.discord_config or {}).get("ask_history", [])
            self.assertFalse(ask_history)

    def test_webhook_issue_deleted_without_prefix_clears_discord_ask_history(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            discord_config = {
                "channel_id": "discord-channel-1",
                "notify_events": ["run_started"],
                "allowed_user_ids": ["u-admin"],
                "ask_history": [
                    {
                        "user_id": "u-viewer",
                        "channel_id": "discord-channel-1",
                        "question": "What changed?",
                        "answer": "Previous answer",
                        "issue_key": "TP-405",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
            tenant.discord_config = discord_config
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-405", labels=["agent:ready"])
        payload["webhookEvent"] = "issue_deleted"

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "issue_deleted")
        self.assertEqual(response.json()["removed_history_entries"], 1)

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            ask_history = (tenant.discord_config or {}).get("ask_history", [])
            self.assertFalse(ask_history)

    def test_comment_run_clears_ask_history_for_issue(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            discord_config = dict(tenant.discord_config or {})
            discord_config["ask_history"] = [
                {
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "question": "Can this run?",
                    "answer": "Needs run command",
                    "issue_key": "TP-406",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ]
            tenant.discord_config = discord_config
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-406")
        payload["webhookEvent"] = "comment_updated"
        payload["comment"] = {
            "author": {"accountId": "jira-user-1"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "/mb run"}],
                    }
                ],
            },
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enqueued"])
        self.assertEqual(response.json()["command"], "run")
        self.assertEqual(response.json()["webhook_event"], "comment_updated")

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            ask_history = (tenant.discord_config or {}).get("ask_history", [])
            self.assertFalse(ask_history)

    def test_comment_webhook_without_mb_command_is_ignored(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            discord_config = dict(tenant.discord_config or {})
            discord_config["ask_history"] = [
                {
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "question": "Can this run?",
                    "answer": "Needs confirmation",
                    "issue_key": "TP-902",
                    "created_at": "2026-01-01T00:00:00+00:00",
                }
            ]
            tenant.discord_config = discord_config
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-902", status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-1"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Can someone take a look?"}],
                    }
                ],
            },
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["enqueued"])
        self.assertEqual(body["reason"], "comment_without_command")
        self.assertEqual(body["webhook_event"], "comment_created")
        self.assertEqual(body["removed_history_entries"], 1)

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            ask_history = (tenant.discord_config or {}).get("ask_history", [])
            self.assertFalse(ask_history)

    def test_comment_updated_without_mb_command_is_ignored(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-903", status_name="To Do")
        payload["webhookEvent"] = "comment_updated"
        payload["comment"] = {
            "author": {"accountId": "jira-user-1"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Updated note without command"}],
                    }
                ],
            },
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["enqueued"])
        self.assertEqual(body["reason"], "comment_without_command")
        self.assertEqual(body["webhook_event"], "comment_updated")
        self.assertEqual(body["removed_history_entries"], 0)

    def test_webhook_marks_transition_into_ready_status(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-128", labels=["agent:ready"])
        payload["changelog"] = {
            "items": [
                {
                    "field": "status",
                    "fromString": "To Do",
                    "toString": "Ready for Agent",
                }
            ]
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enqueued"])
        self.assertEqual(response.json()["trigger_reason"], "status_transition_to_ready")

    def test_webhook_deduplicates_delivery_identifier(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-123"}

        first = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        second = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "duplicate_delivery")
        self.assertEqual(second.json()["run_id"], first.json()["run_id"])

    def test_parse_jira_comment_command_supports_ask_with_inline_and_multiline_text(self) -> None:
        inline_payload = {
            "comment": {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "/mb ask What changed since last run?"}],
                        }
                    ],
                }
            }
        }
        command, argument, error = _parse_jira_comment_command(inline_payload)
        self.assertEqual(command, "ask")
        self.assertEqual(argument, "What changed since last run?")
        self.assertIsNone(error)

        multiline_payload = {
            "comment": {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "/mb ask"}]},
                        {"type": "paragraph", "content": [{"type": "text", "text": "Can you explain the failure?"}]},
                    ],
                }
            }
        }
        command, argument, error = _parse_jira_comment_command(multiline_payload)
        self.assertEqual(command, "ask")
        self.assertEqual(argument, "Can you explain the failure?")
        self.assertIsNone(error)

    def test_webhook_comment_command_ask_posts_reply_and_does_not_enqueue_run(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-901", status_name="To Do")
        payload["comment"] = {
            "author": {"accountId": "jira-user-1"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "/mb ask Can you fix this?"}],
                    }
                ],
            },
        }
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(ok=True, command="ask", message="I can fix this.", data=None),
            ) as command_mock,
            patch("orchestrator.api.routes_webhook._post_jira_comment", return_value=(True, None)) as post_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["enqueued"])
        self.assertEqual(body["reason"], "comment_command_ask")
        self.assertTrue(body["comment_posted"])
        self.assertIsNone(body["comment_error"])
        command_mock.assert_called_once()
        dispatched_payload = command_mock.call_args.kwargs["payload"]
        self.assertIsNone(dispatched_payload.channel_id)
        self.assertNotIn("ingress_source", command_mock.call_args.kwargs)
        post_mock.assert_called_once()

        with self.session_factory() as session:
            run = session.execute(
                select(Run).where(Run.tenant_id == "tenant-webhook", Run.issue_key == "TP-901")
            ).scalar_one_or_none()
            self.assertIsNone(run)

    def test_webhook_comment_command_ask_uses_jira_ingress_contract_with_tenant_discord_scope(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            tenant.discord_config = {"channel_id": "discord-channel-1", "notify_events": []}
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-905", status_name="To Do")
        payload["comment"] = {
            "author": {"accountId": "jira-user-2"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "/mb ask What changed?"}],
                    }
                ],
            },
        }
        with (
            patch(
                "orchestrator.api.routes_discord._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="TP-905", summary="Investigate", status="To Do")],
            ),
            patch("orchestrator.api.routes_discord.build_codex_runtime"),
            patch("orchestrator.api.routes_discord.answer_board_question_with_codex", return_value="Jira ask response"),
            patch("orchestrator.api.routes_webhook._post_jira_comment", return_value=(True, None)) as post_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["reason"], "comment_command_ask")
        self.assertTrue(body["comment_posted"])
        post_mock.assert_called_once()
        posted_comment = post_mock.call_args.kwargs["comment"]
        self.assertEqual(posted_comment, "Jira ask response")

    def test_webhook_respects_tenant_concurrency_limit(self) -> None:
        self._create_tenant("tenant-single", max_concurrent_runs=1)
        first_payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])
        second_payload = self._jira_issue_payload(issue_key="TP-127", labels=["agent:ready"])

        first = self.client.post("/jira/webhook/tenant-single", json=first_payload)
        second = self.client.post("/jira/webhook/tenant-single", json=second_payload)

        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.json()["enqueued"])
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["enqueued"])
        self.assertEqual(second.json()["reason"], "tenant_concurrency_limit_reached")

    def test_webhook_unknown_tenant_returns_404(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-999", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/missing-tenant", json=payload)

        self.assertEqual(response.status_code, 404)

    def test_webhook_requires_valid_token_when_secret_ref_configured(self) -> None:
        self._create_tenant("tenant-auth", webhook_secret_ref=self.webhook_secret_env)
        payload = self._jira_issue_payload(issue_key="TP-125", labels=["agent:ready"])

        unauthenticated = self.client.post("/jira/webhook/tenant-auth", json=payload)
        invalid_token = self.client.post(
            "/jira/webhook/tenant-auth",
            json=payload,
            headers={"X-Webhook-Token": "incorrect"},
        )
        valid_token = self.client.post(
            "/jira/webhook/tenant-auth",
            json=payload,
            headers={"X-Webhook-Token": self.webhook_secret_value},
        )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(invalid_token.status_code, 401)
        self.assertEqual(valid_token.status_code, 200)
        self.assertTrue(valid_token.json()["enqueued"])

    def test_github_webhook_ping_is_accepted(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json={"zen": "keep it logically awesome"},
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "gh-delivery-1",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "ping")

    def test_github_webhook_unknown_installation_is_accepted_without_handler(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json={
                "action": "opened",
                "installation": {"id": 999999},
                "repository": {"full_name": "example/repo"},
            },
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "gh-delivery-2",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "unknown_installation")

    def test_github_webhook_resolves_tenant_from_installation_id(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json={
                "action": "synchronize",
                "installation": {"id": 12345},
                "repository": {"full_name": "example/repo"},
            },
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "gh-delivery-3",
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["tenant_id"], "tenant-webhook")
        self.assertEqual(response.json()["reason"], "missing_pr_context")

    def test_github_webhook_rejects_invalid_signature_when_global_secret_configured(self) -> None:
        self.client.put(
            "/api/admin/secrets/GITHUB_WEBHOOK_SECRET",
            json={"value": self.github_webhook_secret_value},
            auth=("admin", "secret"),
        )
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "example/repo"},
        }
        payload_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": self._sign_github_payload(payload_bytes, "wrong-secret"),
            },
        )

        self.assertEqual(response.status_code, 401)

    def test_github_webhook_accepts_valid_signature_when_global_secret_configured(self) -> None:
        self.client.put(
            "/api/admin/secrets/GITHUB_WEBHOOK_SECRET",
            json={"value": self.github_webhook_secret_value},
            auth=("admin", "secret"),
        )
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "example/repo"},
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_github_payload(payload_bytes, self.github_webhook_secret_value)

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "missing_pr_context")

    def test_github_webhook_enforces_payload_size_limit(self) -> None:
        os.environ["ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES"] = "20"
        payload = {"zen": "abcdefghijklmnopqrstuvwxyz"}
        payload_bytes = json.dumps(payload).encode("utf-8")

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "ping",
            },
        )

        self.assertEqual(response.status_code, 413)

    def test_jira_webhook_uses_managed_secret_ref(self) -> None:
        managed_ref = "secret/jira-managed-token"
        self.client.put(
            "/api/admin/secrets/secret%2Fjira-managed-token",
            json={"value": "managed-jira-token"},
            auth=("admin", "secret"),
        )
        self._create_tenant("tenant-managed-jira", webhook_secret_ref=managed_ref)

        payload = self._jira_issue_payload(issue_key="TP-555", labels=["agent:ready"])
        response = self.client.post(
            "/jira/webhook/tenant-managed-jira",
            json=payload,
            headers={"X-Webhook-Token": "managed-jira-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["enqueued"])

    def test_github_webhook_uses_managed_global_secret_ref(self) -> None:
        self.client.put(
            "/api/admin/secrets/GITHUB_WEBHOOK_SECRET",
            json={"value": "managed-global-secret"},
            auth=("admin", "secret"),
        )
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "example/repo"},
        }
        payload_bytes = json.dumps(payload).encode("utf-8")
        signature = self._sign_github_payload(payload_bytes, "managed-global-secret")

        response = self.client.post(
            "/github/webhook",
            content=payload_bytes,
            headers={
                "content-type": "application/json",
                "X-GitHub-Event": "pull_request",
                "X-Hub-Signature-256": signature,
            },
        )

        self.assertEqual(response.status_code, 202)
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "missing_pr_context")

    def test_discord_webhook_routes_with_discord_ingress_contract(self) -> None:
        with patch(
            "orchestrator.api.routes_webhook_discord.execute_discord_command",
            return_value=DiscordCommandResponse(ok=True, command="help", message="ok", data=None),
        ) as command_mock:
            response = self.client.post(
                "/discord/webhook/tenant-webhook",
                json={"user_id": "discord-user-1", "command": "!help", "channel_id": "discord-channel-1"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        command_mock.assert_called_once()
        self.assertNotIn("ingress_source", command_mock.call_args.kwargs)

    def test_discord_interaction_commands_are_deferred_and_processed_async(self) -> None:
        payload = {
            "type": 2,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"name": "help"},
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        def _capture_and_close(coro):
            coro.close()
            return MagicMock(name="discord-task")

        with (
            patch("orchestrator.api.routes_webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook_discord_interactions._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.routes_webhook_discord_interactions.asyncio.create_task", side_effect=_capture_and_close) as create_task_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        create_task_mock.assert_called_once()

    def test_discord_component_interactions_are_deferred_and_processed_async(self) -> None:
        payload = {
            "type": 3,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"custom_id": "ask.approve.0123456789abcdef0123456789abcdef"},
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        def _capture_and_close(coro):
            coro.close()
            return MagicMock(name="discord-component-task")

        with (
            patch("orchestrator.api.routes_webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook_discord_interactions._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.routes_webhook_discord_interactions.asyncio.create_task", side_effect=_capture_and_close) as create_task_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        create_task_mock.assert_called_once()

    def test_discord_issue_autocomplete_passes_channel_id_for_project_scoping(self) -> None:
        payload = {
            "type": 4,
            "channel_id": "discord-channel-1",
            "data": {
                "name": "run",
                "options": [
                    {"type": 3, "name": "issue_key", "value": "TP", "focused": True},
                ],
            },
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        with (
            patch("orchestrator.api.routes_webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook_discord_interactions._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.routes_webhook_discord_interactions._discord_issue_autocomplete_choices", return_value=[]) as choices_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        choices_mock.assert_called_once()
        self.assertEqual(choices_mock.call_args.kwargs["channel_id"], "discord-channel-1")

    def test_discord_issue_autocomplete_filters_results_locally(self) -> None:
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")
        fake_issues = [
            SimpleNamespace(key="TP-10", summary="Fix auth"),
            SimpleNamespace(key="TP-11", summary="Add notifications"),
            SimpleNamespace(key="ZZ-1", summary="Other project"),
        ]
        with (
            self.session_factory() as session,
            patch("orchestrator.api.routes_webhook._project_filter_jql", return_value='project = "TP"'),
            patch("orchestrator.api.routes_webhook._search_jira_issues_for_tenant", return_value=fake_issues),
        ):
            choices = _discord_issue_autocomplete_choices(
                session=session,
                tenant=fake_tenant,  # type: ignore[arg-type]
                channel_id="discord-channel-1",
                current_value="TP-11",
            )
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["value"], "TP-11")

    def test_parse_discord_bug_interaction_includes_params_and_attachments(self) -> None:
        payload = {
            "type": 2,
            "channel_id": "discord-channel-1",
            "member": {"user": {"id": "discord-user-1"}},
            "data": {
                "name": "bug",
                "options": [
                    {"type": 3, "name": "summary", "value": "Login fails"},
                    {"type": 3, "name": "details", "value": "Spinner never stops"},
                    {"type": 3, "name": "issue_key", "value": "TP-11"},
                    {"type": 11, "name": "attachment_1", "value": "att-1"},
                ],
                "resolved": {
                    "attachments": {
                        "att-1": {
                            "id": "att-1",
                            "url": "https://cdn.discordapp.com/attachments/att-1.png",
                            "filename": "att-1.png",
                            "content_type": "image/png",
                        }
                    }
                },
            },
        }

        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload)
        self.assertEqual(user_id, "discord-user-1")
        self.assertEqual(channel_id, "discord-channel-1")
        self.assertEqual(command_text, "!bug Login fails")
        self.assertEqual(command_params["summary"], "Login fails")
        self.assertEqual(command_params["details"], "Spinner never stops")
        self.assertEqual(command_params["issue_key"], "TP-11")
        self.assertEqual(attachments[0]["filename"], "att-1.png")

    def test_parse_discord_gap_interaction_includes_issue_key_param(self) -> None:
        payload = {
            "type": 2,
            "channel_id": "discord-channel-1",
            "member": {"user": {"id": "discord-user-1"}},
            "data": {
                "name": "gap",
                "options": [
                    {"type": 3, "name": "issue_key", "value": "TP-44"},
                ],
            },
        }

        user_id, channel_id, command_text, command_params, attachments = _parse_discord_interaction_command(payload)
        self.assertEqual(user_id, "discord-user-1")
        self.assertEqual(channel_id, "discord-channel-1")
        self.assertEqual(command_text, "!gap TP-44")
        self.assertEqual(command_params, {"issue_key": "TP-44"})
        self.assertEqual(attachments, [])

    def test_discord_reply_button_component_returns_modal(self) -> None:
        payload = {
            "type": 3,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"custom_id": "ask.reply.open"},
            "message": {"id": "123456789012345678"},
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")

        with (
            patch("orchestrator.api.routes_webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook_discord_interactions._find_tenant_for_discord_channel", return_value=fake_tenant),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 9)
        self.assertEqual(body["data"]["custom_id"], "ask.reply.123456789012345678")
        self.assertEqual(body["data"]["components"][0]["components"][0]["custom_id"], "question")

    def test_discord_reply_message_command_returns_modal(self) -> None:
        payload = {
            "type": 2,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {
                "name": "reply",
                "type": 3,
                "target_id": "123456789012345678",
                "resolved": {
                    "messages": {
                        "123456789012345678": {
                            "id": "123456789012345678",
                            "author": {"id": "discord-app-1"},
                        }
                    }
                },
            },
            "member": {"user": {"id": "discord-user-1"}},
        }

        with (
            patch("orchestrator.api.routes_webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook_discord_interactions._validate_discord_interaction_signature"),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 9)
        self.assertEqual(body["data"]["custom_id"], "ask.reply.123456789012345678")
        self.assertEqual(body["data"]["components"][0]["components"][0]["custom_id"], "question")

    def test_discord_reply_modal_submit_is_deferred_and_processed_async(self) -> None:
        payload = {
            "type": 5,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {
                "custom_id": "ask.reply.123456789012345678",
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": "question",
                                "value": "What changed since the previous update?",
                            }
                        ],
                    }
                ],
            },
            "member": {"user": {"id": "discord-user-1"}},
        }
        fake_tenant = SimpleNamespace(tenant_id="tenant-webhook")
        run_followup_mock = MagicMock()

        with (
            patch("orchestrator.api.routes_webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes_webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.routes_webhook_discord_interactions._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.routes_webhook_discord_interactions._run_discord_command_followup", run_followup_mock),
            patch("orchestrator.api.routes_webhook_discord_interactions.asyncio.create_task", return_value=MagicMock()) as create_task_mock,
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        create_task_mock.assert_called_once()
        run_followup_mock.assert_called_once_with(
            tenant_id="tenant-webhook",
            user_id="discord-user-1",
            channel_id="discord-channel-1",
            command_text="!ask What changed since the previous update?",
            application_id="discord-app-1",
            interaction_token="interaction-token-1",
            reply_to_message_id="123456789012345678",
        )

    def test_discord_followup_formats_issue_and_pr_references_as_hyperlinks(self) -> None:
        with self.session_factory() as session:
            tenant = session.execute(
                select(Tenant).where(Tenant.tenant_id == "tenant-webhook")
            ).scalar_one()
            message = _build_command_followup_message(
                session=session,
                tenant=tenant,
                user_id="discord-user-1",
                command_response=DiscordCommandResponse(
                    ok=True,
                    command="link",
                    message="Links for TP-999",
                    data={
                        "issue_key": "TP-999",
                        "jira_url": "https://master-builder.atlassian.net/browse/TP-999",
                        "pr_url": "https://github.com/example/repo/pull/77",
                    },
                ),
            )

        self.assertIn("[TP-999](https://master-builder.atlassian.net/browse/TP-999)", message)
        self.assertIn("[Open PR](https://github.com/example/repo/pull/77)", message)

    def test_discord_reply_followup_posts_to_thread_without_webhook_followup(self) -> None:
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch("orchestrator.api.routes_webhook._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_interaction_followup") as interaction_send_mock,
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!ask Can you fix it?",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                    reply_to_message_id="123456789012345678",
                )
            )

        thread_send_mock.assert_called_once()
        interaction_send_mock.assert_not_called()

    def test_discord_interaction_followup_send_failure_is_swallowed(self) -> None:
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="help",
                    message="ok",
                    data=None,
                ),
            ),
            patch(
                "orchestrator.api.routes_webhook._send_discord_interaction_followup",
                side_effect=RuntimeError("token expired"),
            ),
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!help",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

    def test_discord_followup_executes_with_discord_ingress_contract(self) -> None:
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(ok=True, command="help", message="ok", data=None),
            ) as command_mock,
            patch("orchestrator.api.routes_webhook._send_discord_interaction_followup"),
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!help",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

        self.assertEqual(command_mock.call_count, 1)
        self.assertEqual(command_mock.call_args.kwargs["ingress_source"], "discord")

    def test_discord_ask_followup_creates_new_thread_for_initial_response(self) -> None:
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch("orchestrator.api.routes_webhook._send_discord_ask_response_with_thread") as ask_thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_interaction_followup") as interaction_send_mock,
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!ask Can you fix it?",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

        ask_thread_send_mock.assert_called_once()
        thread_send_mock.assert_not_called()
        interaction_send_mock.assert_not_called()

    def test_send_discord_thread_followup_falls_back_to_current_channel_when_thread_lookup_fails(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)

            fake_client = MagicMock()
            fake_client.ensure_thread_for_message.side_effect = DiscordApiError("Cannot create nested thread")
            with (
                patch("orchestrator.api.routes_webhook.resolve_scoped_secret_ref", return_value="bot-token"),
                patch("orchestrator.api.routes_webhook.DiscordApiClient", return_value=fake_client),
            ):
                _send_discord_thread_followup(
                    session=session,
                    settings=get_settings(),
                    tenant=tenant,
                    channel_id="discord-thread-1",
                    reply_to_message_id="123456789012345678",
                    content="reply content",
                )

            fake_client.post_message.assert_called_once_with(
                channel_id="discord-thread-1",
                content="reply content",
                components=None,
            )

    def test_send_discord_thread_followup_posts_directly_for_known_thread_channel(self) -> None:
        with self.session_factory() as session:
            project = session.get(Project, "tenant-webhook-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-thread-1"]
            project.discord_config = discord_config
            session.commit()

            fake_client = MagicMock()
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            with (
                patch("orchestrator.api.routes_webhook.resolve_scoped_secret_ref", return_value="bot-token"),
                patch("orchestrator.api.routes_webhook.DiscordApiClient", return_value=fake_client),
            ):
                _send_discord_thread_followup(
                    session=session,
                    settings=get_settings(),
                    tenant=tenant,
                    channel_id="discord-thread-1",
                    reply_to_message_id="123456789012345678",
                    content="reply content",
                )

            fake_client.ensure_thread_for_message.assert_not_called()
            fake_client.post_message.assert_called_once_with(
                channel_id="discord-thread-1",
                content="reply content",
                components=None,
            )

    def test_discord_issues_followup_creates_seed_thread_for_clarifications(self) -> None:
        with (
            patch(
                "orchestrator.api.routes_webhook.execute_discord_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="issues",
                    message="Issue upsert complete. I still need more detail.",
                    data={
                        "requires_input": True,
                        "followup_request_id": "req-123",
                        "questions": ["What rollout plan should we use?"],
                    },
                ),
            ),
            patch("orchestrator.api.routes_webhook._send_discord_seed_followup_with_thread") as seed_thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.routes_webhook._send_discord_interaction_followup") as interaction_send_mock,
        ):
            asyncio.run(
                _run_discord_command_followup(
                    tenant_id="tenant-webhook",
                    user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    command_text="!issues seed Build API and worker stories",
                    application_id="discord-app-1",
                    interaction_token="interaction-token-1",
                )
            )

        seed_thread_send_mock.assert_called_once()
        thread_send_mock.assert_not_called()
        interaction_send_mock.assert_not_called()

    def test_find_tenant_for_discord_channel_matches_registered_thread_channel(self) -> None:
        with self.session_factory() as session:
            project = session.get(Project, "tenant-webhook-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-thread-123"]
            project.discord_config = discord_config
            session.commit()

            matched = _find_tenant_for_discord_channel(session=session, channel_id="discord-thread-123")
            self.assertIsNotNone(matched)
            self.assertEqual(matched.tenant_id, "tenant-webhook")

    def test_find_tenant_for_discord_channel_matches_seed_followup_thread_channel(self) -> None:
        with self.session_factory() as session:
            project = session.get(Project, "tenant-webhook-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["seed_followup_thread_channel_ids"] = ["discord-thread-seed-1"]
            project.discord_config = discord_config
            session.commit()

            matched = _find_tenant_for_discord_channel(session=session, channel_id="discord-thread-seed-1")
            self.assertIsNotNone(matched)
            self.assertEqual(matched.tenant_id, "tenant-webhook")

    def test_find_tenant_for_discord_channel_matches_project_discord_channel(self) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            self.assertIsNotNone(project)
            project.discord_config = {
                "channel_id": "discord-project-channel-1",
                "ask_thread_channel_ids": ["discord-project-thread-1"],
            }
            session.commit()

            matched = _find_tenant_for_discord_channel(session=session, channel_id="discord-project-thread-1")
            self.assertIsNotNone(matched)
            self.assertEqual(matched.tenant_id, "tenant-webhook")

    def test_find_tenant_for_discord_channel_returns_none_when_channel_is_shared(self) -> None:
        self._create_tenant("tenant-webhook-2")
        with self.session_factory() as session:
            project_one = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            project_two = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook-2",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            assert project_one is not None
            assert project_two is not None
            project_one.discord_config = {"channel_id": "discord-shared-channel"}
            project_two.discord_config = {"channel_id": "discord-shared-channel"}
            session.commit()

            matched = _find_tenant_for_discord_channel(session=session, channel_id="discord-shared-channel")
            self.assertIsNone(matched)

    def test_find_tenant_for_discord_channel_refreshes_after_project_channel_change(self) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project).where(
                    Project.tenant_id == "tenant-webhook",
                    Project.is_archived.is_(False),
                )
            ).scalar_one_or_none()
            assert project is not None
            project.discord_config = {"channel_id": "discord-dynamic-1"}
            session.commit()

            first_match = _find_tenant_for_discord_channel(session=session, channel_id="discord-dynamic-1")
            self.assertIsNotNone(first_match)
            assert first_match is not None
            self.assertEqual(first_match.tenant_id, "tenant-webhook")

            project.discord_config = {"channel_id": "discord-dynamic-2"}
            session.commit()

            stale_match = _find_tenant_for_discord_channel(session=session, channel_id="discord-dynamic-1")
            self.assertIsNone(stale_match)

            refreshed_match = _find_tenant_for_discord_channel(session=session, channel_id="discord-dynamic-2")
            self.assertIsNotNone(refreshed_match)
            assert refreshed_match is not None
            self.assertEqual(refreshed_match.tenant_id, "tenant-webhook")
