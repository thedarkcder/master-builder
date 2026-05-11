import os
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext
from orchestrator.api.routes.webhook import (
    _parse_jira_comment_command,
)
from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.webhooks.health import webhook_health_tracker
from orchestrator.storage.models import FollowupContext, Project, Run, Tenant, WebhookJob
from orchestrator.tools.atlassian_oauth import JiraIssuePreview, AtlassianOAuthError
from tests.test_support.jira_webhook_api_harness import JiraWebhookTestsHarness

pytestmark = pytest.mark.contract


class JiraWebhookTests(JiraWebhookTestsHarness):

    def test_webhook_ignores_disabled_tenant(self) -> None:
        self._create_tenant("tenant-disabled", is_enabled=False)
        payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/tenant-disabled", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enqueued"])
        self.assertEqual(response.json()["reason"], "tenant_disabled")

    def test_webhook_queues_todo_status_for_worker_reconciliation(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-123", status_name="To Do", labels=["agent:ready"])

        with patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["accepted"])
        self.assertEqual(response.json()["reason"], "queued_for_reconciliation")
        with self.session_factory() as session:
            jobs = session.execute(select(WebhookJob)).scalars().all()
            runs = session.execute(select(Run)).scalars().all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].status, "pending")
        self.assertEqual(jobs[0].subject_key, "jira:tenant-webhook:TP-123")
        self.assertEqual(runs, [])

    def test_webhook_queues_comment_event_for_worker_reconciliation(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-123", status_name="To Do", labels=["agent:ready"])
        payload["webhookEvent"] = "comment_created"
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

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-123")
        self.assertEqual(body["webhook_event"], "comment_created")
        with self.session_factory() as session:
            jobs = session.execute(select(WebhookJob)).scalars().all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].transport, "jira_webhook")

    def test_webhook_does_not_enqueue_when_issue_is_in_backlog_for_configured_board_on_create_sends_notification(self) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project)
                .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                .limit(1)
            ).scalar_one()
            project.policy_overrides = {**dict(project.policy_overrides or {}), "run_board_id": 1}
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-123", status_name="To Do", labels=["agent:ready"])
        payload["webhookEvent"] = "jira:issue_created"
        with (
            patch("orchestrator.api.webhooks.jira_board_location._fetch_issue_board_location", return_value=("backlog", None)),
            patch(
                "orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check",
                return_value=self._pre_run_check(outcome="decision_gate_required"),
            ),
            patch("orchestrator.core.discord.transport_executor.send_tenant_discord_message") as notify_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-123")
        self.assertEqual(body["webhook_event"], "issue_created")
        notify_mock.assert_not_called()

    def test_webhook_does_not_notify_backlog_message_for_non_create_event(self) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project)
                .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                .limit(1)
            ).scalar_one()
            project.policy_overrides = {**dict(project.policy_overrides or {}), "run_board_id": 1}
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-139", status_name="To Do", labels=["agent:ready"])
        payload["webhookEvent"] = "jira:issue_updated"
        with (
            patch("orchestrator.api.webhooks.jira_board_location._fetch_issue_board_location", return_value=("backlog", None)),
            patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()),
            patch("orchestrator.core.discord.transport_executor.send_tenant_discord_message") as notify_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-139")
        self.assertEqual(body["webhook_event"], "issue_updated")
        notify_mock.assert_not_called()

    def test_webhook_applies_required_worker_label_using_pre_run_check(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-126", status_name="To Do", labels=["agent:ready"])
        payload["issue"]["fields"]["summary"] = "Build iOS app shell"
        payload["issue"]["fields"]["description"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Objective: implement SwiftUI onboarding. How to test: Xcode build."}],
                }
            ],
        }
        oauth_client = MagicMock()
        oauth_context = SimpleNamespace(
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1"),
            client=oauth_client,
        )
        with patch(
            "orchestrator.api.webhooks.jira_admission_flow.tenant_atlassian_oauth_context",
            return_value=oauth_context,
        ), patch(
            "orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check",
            return_value=self._pre_run_check(capability="macos"),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-126")
        oauth_client.add_issue_labels.assert_not_called()

    def test_webhook_does_not_enqueue_when_issue_is_not_on_configured_board(self) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project)
                .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                .limit(1)
            ).scalar_one()
            project.policy_overrides = {**dict(project.policy_overrides or {}), "run_board_id": 1}
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-123", status_name="To Do", labels=["agent:ready"])
        with patch("orchestrator.api.webhooks.jira_board_location._fetch_issue_board_location", return_value=("not_on_board", None)):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-123")
        self.assertEqual(body["webhook_event"], "issue_updated")

    def test_webhook_enqueues_when_issue_is_on_configured_board(self) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project)
                .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                .limit(1)
            ).scalar_one()
            project.policy_overrides = {**dict(project.policy_overrides or {}), "run_board_id": 1}
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-123", status_name="To Do", labels=["agent:ready"])
        with patch("orchestrator.api.webhooks.jira_board_location._fetch_issue_board_location", return_value=("board", None)):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-123")

    def test_fetch_issue_board_location_still_checks_board_when_backlog_lookup_fails(self) -> None:
        from orchestrator.api.webhooks.jira_board_location import _fetch_issue_board_location

        context = JiraWebhookContext(
            request_id="req-1",
            tenant_id="tenant-webhook",
            tenant=SimpleNamespace(tenant_id="tenant-webhook", jira_config={}),
            payload={},
            webhook_event="issue_updated",
            issue_key="TP-123",
            issue_labels=[],
            issue_status="To Do",
            issue_status_category_key="indeterminate",
            issue_summary="Summary",
            issue_description="Description",
            comment_command=None,
            comment_command_argument=None,
            comment_command_error=None,
            delivery_id=None,
            project=None,
        )
        oauth_context = SimpleNamespace(
            connection=SimpleNamespace(cloud_id="cloud-1"),
            access_token="token",
        )
        http_client = MagicMock()
        http_client.get_json.side_effect = [
            AtlassianOAuthError("backlog endpoint unavailable"),
            {"issues": [{"key": "TP-123"}]},
        ]

        with (
            patch("orchestrator.api.webhooks.jira_board_location.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_board_location.AtlassianOAuthHttpClient", return_value=http_client),
        ):
            location, detail = _fetch_issue_board_location(
                context=context,
                session=MagicMock(),
                settings=SimpleNamespace(),
                board_id=1,
            )

        self.assertEqual(location, "board")
        self.assertIsNone(detail)

    def test_webhook_enqueues_issue_created_event_in_todo(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-130", status_name="To Do", labels=["agent:ready"])
        payload["webhookEvent"] = "jira:issue_created"

        with patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-130")
        self.assertEqual(body["webhook_event"], "issue_created")

    def test_webhook_ignores_done_issue_status(self) -> None:
        payload = self._jira_issue_payload(
            issue_key="TP-123",
            status_name="Done",
            status_category_key="done",
            labels=["agent:ready"],
        )

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-123")

    def test_webhook_enqueues_once_for_active_issue(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-124", labels=["agent:ready"])
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-active-issue-124"}

        first = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        second = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)

        first_body = self._assert_jira_issue_event_queued(first, issue_key="TP-124")
        second_body = self._assert_jira_issue_event_queued(
            second,
            issue_key="TP-124",
            reason="duplicate_delivery",
            queued=False,
        )
        self.assertEqual(second_body["job_id"], first_body["job_id"])

    def test_webhook_enqueue_skip_sends_discord_reason(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-124", labels=["agent:ready"])
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-duplicate-124"}
        first = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        self._assert_jira_issue_event_queued(first, issue_key="TP-124")

        with patch("orchestrator.core.discord.transport_executor.send_tenant_discord_message") as notify_mock:
            second = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)

        self._assert_jira_issue_event_queued(
            second,
            issue_key="TP-124",
            reason="duplicate_delivery",
            queued=False,
        )
        notify_mock.assert_not_called()

    def test_webhook_records_last_delivery_metadata(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-777", labels=["agent:ready"])
        payload["webhookEvent"] = "jira:issue_updated"
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-meta-1"}

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        self._assert_jira_issue_event_queued(response, issue_key="TP-777")

        tenant_response = self.client.get("/api/admin/tenants/tenant-webhook", auth=("admin", "secret"))
        self.assertEqual(tenant_response.status_code, 200)
        jira_config = tenant_response.json()["jira"]
        self.assertEqual(jira_config["webhook_last_delivery_id"], "delivery-meta-1")
        self.assertEqual(jira_config["webhook_last_issue_key"], "TP-777")
        self.assertIsNotNone(jira_config["webhook_last_received_at"])
        self.assertEqual(jira_config["webhook_last_event"], "issue_updated")

    def test_webhook_issue_deleted_clears_discord_ask_history(self) -> None:
        now = datetime.now(timezone.utc)
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
            session.add(
                FollowupContext(
                    context_id="seed-followup-1",
                    tenant_id="tenant-webhook",
                    project_id=None,
                    context_type="seed_followup",
                    status="active",
                    channel_id=None,
                    thread_channel_id="seed-thread-1",
                    root_message_id=None,
                    issue_key="TP-404",
                    request_id="req-1",
                    run_id=None,
                    metadata_json={
                        "request_id": "req-1",
                        "user_id": "u-viewer",
                        "channel_ids": ["seed-thread-1"],
                        "issue_keys": ["TP-404"],
                        "questions": ["Need rollout plan"],
                        "prompt_markdown": "Seed prompt",
                        "updated_at": now.isoformat(),
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-404", labels=["agent:ready"])
        payload["webhookEvent"] = "jira:issue_deleted"

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-404")
        processed = self._process_one_webhook_job()
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)
            ask_history = (tenant.discord_config or {}).get("ask_history", [])
            self.assertFalse(ask_history)
            seed_followups = (
                session.execute(
                    select(FollowupContext)
                    .where(FollowupContext.tenant_id == "tenant-webhook", FollowupContext.request_id == "req-1")
                )
                .scalars()
                .all()
            )
            self.assertEqual(len(seed_followups), 1)
            self.assertEqual(seed_followups[0].status, "closed")

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

        self._assert_jira_issue_event_queued(response, issue_key="TP-405")
        processed = self._process_one_webhook_job()
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

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
        self._assert_jira_issue_event_queued(response, issue_key="TP-406")
        processed = self._process_one_webhook_job()
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

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
        self._assert_jira_issue_event_queued(response, issue_key="TP-902")
        processed = self._process_one_webhook_job()
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

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
        self._assert_jira_issue_event_queued(response, issue_key="TP-903")
        processed = self._process_one_webhook_job()
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        with self.session_factory() as session:
            run = session.execute(
                select(Run).where(Run.tenant_id == "tenant-webhook", Run.issue_key == "TP-903")
            ).scalar_one_or_none()
            self.assertIsNone(run)

    def test_webhook_marks_transition_into_todo_status(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-128", labels=["agent:ready"])
        payload["changelog"] = {
            "items": [
                {
                    "field": "status",
                    "fromString": "Backlog",
                    "toString": "To Do",
                }
            ]
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-128")

    def test_webhook_transition_only_mode_ignores_status_recheck_without_transition(self) -> None:
        self._create_tenant("tenant-transition-only", ready_trigger_mode="transition_only")
        payload = self._jira_issue_payload(issue_key="TP-129", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/tenant-transition-only", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-129")

    def test_webhook_ignores_backlog_followup_issue_created_event(self) -> None:
        payload = self._jira_issue_payload(
            issue_key="TP-130",
            labels=["backlog-only"],
            status_name="Ready for Agent",
        )
        payload["webhookEvent"] = "jira:issue_created"

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-130")
        self.assertEqual(body["webhook_event"], "issue_created")

    def test_webhook_allows_manual_run_command_path_for_backlog_followup_label(self) -> None:
        payload = self._jira_issue_payload(
            issue_key="TP-131",
            labels=["backlog-only"],
            status_name="Ready for Agent",
        )
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

        with patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-131")
        processed = self._process_one_webhook_job()
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        with self.session_factory() as session:
            run = session.execute(
                select(Run).where(Run.tenant_id == "tenant-webhook", Run.issue_key == "TP-131")
            ).scalar_one_or_none()
            self.assertIsNone(run)

    def test_webhook_deduplicates_delivery_identifier(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])
        headers = {"X-Atlassian-Webhook-Identifier": "delivery-123"}

        first = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)
        second = self.client.post("/jira/webhook/tenant-webhook", json=payload, headers=headers)

        first_body = self._assert_jira_issue_event_queued(first, issue_key="TP-126")
        second_body = self._assert_jira_issue_event_queued(
            second,
            issue_key="TP-126",
            reason="duplicate_delivery",
            queued=False,
        )
        self.assertEqual(second_body["job_id"], first_body["job_id"])

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
        payload["webhookEvent"] = "comment_created"
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
                "orchestrator.api.webhooks.jira_webhook_comment_flow.execute_jira_comment_command",
                return_value=DiscordCommandResponse(ok=True, command="ask", message="I can fix this.", data=None),
            ) as command_mock,
            patch("orchestrator.api.webhooks.jira_webhook_comment_flow.post_jira_comment", return_value=(True, None)) as post_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-901")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
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
        payload["webhookEvent"] = "comment_created"
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
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="TP-905", summary="Investigate", status="To Do")],
            ),
            patch("orchestrator.api.discord.ingress.ask_runtime.build_codex_runtime"),
            patch("orchestrator.api.discord.ingress.ask_runtime.answer_board_question_with_runtime", return_value="Jira ask response"),
            patch("orchestrator.api.webhooks.jira_webhook_comment_flow.post_jira_comment", return_value=(True, None)) as post_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-905")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        post_mock.assert_called_once()
        posted_comment = post_mock.call_args.kwargs["comment"]
        self.assertEqual(posted_comment, "Jira ask response")

    def test_webhook_respects_tenant_concurrency_limit(self) -> None:
        self._create_tenant("tenant-single", max_concurrent_runs=1)
        first_payload = self._jira_issue_payload(issue_key="TP-126", labels=["agent:ready"])
        second_payload = self._jira_issue_payload(issue_key="TP-127", labels=["agent:ready"])

        first = self.client.post("/jira/webhook/tenant-single", json=first_payload)
        second = self.client.post("/jira/webhook/tenant-single", json=second_payload)

        self._assert_jira_issue_event_queued(first, issue_key="TP-126")
        self._assert_jira_issue_event_queued(second, issue_key="TP-127")

    def test_webhook_unknown_tenant_returns_404(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-999", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/missing-tenant", json=payload)

        self.assertEqual(response.status_code, 404)

    def test_webhook_unknown_tenant_does_not_record_health_metrics(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-998", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/missing-tenant", json=payload)

        self.assertEqual(response.status_code, 404)
        rollup = webhook_health_tracker.rollup(tenant_id="missing-tenant")
        self.assertEqual(rollup["received_total"], 0.0)
        self.assertEqual(rollup["failed_total"], 0.0)

    def test_webhook_requires_valid_token_when_secret_ref_configured(self) -> None:
        managed_ref = "tenant/tenant-auth/JIRA_WEBHOOK_TOKEN"
        self._create_tenant("tenant-auth", webhook_secret_ref=managed_ref)
        self.client.put(
            "/api/admin/tenants/tenant-auth/secrets/JIRA_WEBHOOK_TOKEN",
            json={"value": self.webhook_secret_value},
            auth=("admin", "secret"),
        )
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
        self._assert_jira_issue_event_queued(valid_token, issue_key="TP-125")

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

    def test_github_push_webhook_enqueues_deployment_event(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json={
                "ref": "refs/heads/main",
                "after": "abcdef1234567890",
                "installation": {"id": 12345},
                "repository": {"full_name": "example/repo"},
            },
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "gh-deploy-1",
            },
        )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertTrue(body["queued"])
        self.assertEqual(body["branch"], "main")
        self.assertEqual(body["commit_sha"], "abcdef1234567890")
        with self.session_factory() as session:
            job = session.execute(select(WebhookJob)).scalar_one()
        self.assertEqual(job.transport, "github_deployment_webhook")
        self.assertEqual(job.context_json["branch"], "main")
        self.assertEqual(job.context_json["commit_sha"], "abcdef1234567890")

    def test_github_webhook_rejects_invalid_signature_when_global_secret_configured(self) -> None:
        self.client.put(
            "/api/admin/secrets/platform%2FGITHUB_WEBHOOK_SECRET",
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
            "/api/admin/secrets/platform%2FGITHUB_WEBHOOK_SECRET",
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
        managed_ref = "tenant/tenant-managed-jira/JIRA_WEBHOOK_TOKEN"
        self._create_tenant("tenant-managed-jira", webhook_secret_ref=managed_ref)
        self.client.put(
            "/api/admin/tenants/tenant-managed-jira/secrets/JIRA_WEBHOOK_TOKEN",
            json={"value": "managed-jira-token"},
            auth=("admin", "secret"),
        )

        payload = self._jira_issue_payload(issue_key="TP-555", labels=["agent:ready"])
        response = self.client.post(
            "/jira/webhook/tenant-managed-jira",
            json=payload,
            headers={"X-Webhook-Token": "managed-jira-token"},
        )
        self._assert_jira_issue_event_queued(response, issue_key="TP-555")

    def test_github_webhook_uses_managed_global_secret_ref(self) -> None:
        self.client.put(
            "/api/admin/secrets/platform%2FGITHUB_WEBHOOK_SECRET",
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
        response = self.client.post(
            "/discord/webhook/tenant-webhook",
            json={"user_id": "discord-user-1", "command": "!help", "channel_id": "discord-channel-1"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertTrue(response.json()["deferred"])
        self.assertTrue(response.json()["queued"])
        with self.session_factory() as session:
            jobs = session.execute(
                select(WebhookJob).where(WebhookJob.transport == "discord_webhook")
            ).scalars().all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].subject_key, "discord_channel:tenant-webhook:discord-channel-1")

    def test_discord_interaction_commands_are_queued_after_immediate_ack(self) -> None:
        payload = {
            "type": 2,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"name": "help"},
            "member": {"user": {"id": "discord-user-1"}},
        }

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
            patch(
                "orchestrator.api.routes.webhook_discord_interactions._resolve_interaction_subject_scope",
                return_value=("tenant-webhook", None, "discord_channel:tenant-webhook:discord-channel-1"),
            ),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 5)
        self.assertEqual(body["data"]["flags"], 64)
        with self.session_factory() as session:
            jobs = session.execute(
                select(WebhookJob).where(WebhookJob.transport == "discord_interaction")
            ).scalars().all()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].tenant_id, "tenant-webhook")
        self.assertIsNone(jobs[0].dedupe_key)
