from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from orchestrator.storage.models import FollowupContext
from orchestrator.tools.jira_oauth import JiraIssueDetail
from tests.test_support.jira_webhook_harness import JiraWebhookHarness


pytestmark = pytest.mark.contract


class JiraParentClarificationFlowTests(JiraWebhookHarness):
    def test_webhook_comment_command_clarify_creates_parent_followup_and_blocks_child(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-960", labels=["engineering-child", "parent-tp-950"], status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-5"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "/mb clarify What should the customer see when retry succeeds?"}],
                    }
                ],
            },
        }

        class _FakeClient:
            def __init__(self) -> None:
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):  # noqa: ANN003
                issue_key = kwargs["issue_id_or_key"]
                if issue_key == "TP-960":
                    return JiraIssueDetail(
                        key="TP-960",
                        summary="Retry UI behavior",
                        status="To Do",
                        description=(
                            "Technical Objective\nRetry UI\nParent Feature Link\nTP-950: Checkout recovery\n"
                            "Behavior Slice\nCustomer-facing retry success message"
                        ),
                        labels=["engineering-child", "parent-tp-950", "sync-current"],
                    )
                return JiraIssueDetail(
                    key="TP-950",
                    summary="Checkout recovery",
                    status="To Do",
                    description="Objective\nCheckout recovery parent",
                    labels=["pm-parent", "sync-current"],
                )

            def replace_issue_labels(self, **kwargs):  # noqa: ANN003
                self.replaced_labels.append(kwargs)
                return None

        client = _FakeClient()
        oauth_context = SimpleNamespace(
            client=client,
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.classify_engineering_clarification_with_codex",
                return_value={
                    "classification": "product_behavior",
                    "stakeholder_question": "When checkout retry succeeds after a recovery, what should the customer see?",
                    "child_block_note": "Need a PM decision on the customer-facing outcome.",
                    "reason": "The current brief leaves the user-facing behavior open.",
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.resolve_parent_feature_case",
                return_value=SimpleNamespace(
                    request_id="pm-parent-950",
                    source_kind="jira_parent",
                    owner_user_id="discord-user-1",
                    channel_id="discord-channel-1",
                    thread_channel_id="discord-thread-1",
                    root_message_id="discord-root-1",
                    source_text="Checkout recovery parent",
                    brief_json={"objective": "Checkout recovery"},
                    notes_json={},
                ),
            ),
            patch("orchestrator.core.jira_parent_child_sync_service.resolve_platform_secret_ref", return_value="discord-token"),
            patch("orchestrator.core.jira_parent_child_sync_service.DiscordApiClient") as discord_client_cls,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
        ):
            discord_client = discord_client_cls.return_value
            discord_client.post_message.return_value = {"id": "discord-msg-1"}
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-960")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(len(client.replaced_labels), 1)
        self.assertIn("sync-blocked", client.replaced_labels[0]["labels"])
        self.assertGreaterEqual(comment_mock.call_count, 2)
        discord_client.post_message.assert_called_once()
        self.assertEqual(discord_client.post_message.call_args.kwargs["channel_id"], "discord-thread-1")
        self.assertIn("customer see", discord_client.post_message.call_args.kwargs["content"])
        with self.session_factory() as session:
            context = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-950",
                    FollowupContext.context_type == "engineering_clarification",
                )
            ).scalar_one_or_none()
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.status, "active")

    def test_webhook_parent_comment_resolves_engineering_clarification_and_closes_context(self) -> None:
        with self.session_factory() as session:
            session.add(
                FollowupContext(
                    context_id="ctx-clarify-1",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="engineering_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id=None,
                    issue_key="TP-950",
                    request_id="engineering-clarification:TP-950",
                    run_id=None,
                    metadata_json={
                        "affected_child_keys": ["TP-960"],
                        "questions": [
                            {
                                "source_child_key": "TP-960",
                                "original_question": "What should the customer see when retry succeeds?",
                                "stakeholder_question": "When checkout retry succeeds after a recovery, what should the customer see?",
                            }
                        ],
                    },
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-950", labels=["pm-parent"], status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-6"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Show a success banner and restore the cart summary."}],
                    }
                ],
            },
        }

        class _FakeClient:
            def get_issue_detail(self, **kwargs):  # noqa: ANN003
                issue_key = kwargs["issue_id_or_key"]
                if issue_key == "TP-950":
                    return JiraIssueDetail(
                        key="TP-950",
                        summary="Checkout recovery",
                        status="To Do",
                        description="Objective\nCheckout recovery parent",
                        labels=["pm-parent", "sync-blocked"],
                    )
                return JiraIssueDetail(
                    key="TP-960",
                    summary="Retry UI behavior",
                    status="To Do",
                    description="Technical Objective\nRetry UI\nParent Feature Link\nTP-950: Checkout recovery",
                    labels=["engineering-child", "parent-tp-950", "sync-blocked"],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "updated",
                    {
                        "updated_parent": "TP-950",
                        "updated_children": ["TP-960"],
                        "created_children": [],
                        "requires_input": False,
                        "parent_revision": "rev-456",
                        "children_sync_status": "children_current",
                    },
                ),
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-950")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertGreaterEqual(comment_mock.call_count, 2)
        with self.session_factory() as session:
            context = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-950",
                    FollowupContext.context_type == "engineering_clarification",
                )
            ).scalar_one_or_none()
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.status, "closed")

    def test_webhook_parent_comment_resolution_allows_new_child_creation(self) -> None:
        with self.session_factory() as session:
            session.add(
                FollowupContext(
                    context_id="ctx-clarify-2",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="engineering_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id=None,
                    issue_key="TP-955",
                    request_id="engineering-clarification:TP-955",
                    run_id=None,
                    metadata_json={
                        "affected_child_keys": ["TP-956"],
                        "questions": [
                            {
                                "source_child_key": "TP-956",
                                "original_question": "Should we also track manual fallback?",
                                "stakeholder_question": "Should manual fallback be included in this feature?",
                            }
                        ],
                    },
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-955", labels=["pm-parent"], status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-6"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Yes, include manual fallback and tracking."}],
                    }
                ],
            },
        }

        class _FakeClient:
            def get_issue_detail(self, **kwargs):  # noqa: ANN003
                issue_key = kwargs["issue_id_or_key"]
                if issue_key == "TP-955":
                    return JiraIssueDetail(
                        key="TP-955",
                        summary="Checkout recovery",
                        status="To Do",
                        description="Objective\nCheckout recovery parent",
                        labels=["pm-parent", "sync-blocked"],
                    )
                return JiraIssueDetail(
                    key="TP-956",
                    summary="Retry UI behavior",
                    status="To Do",
                    description="Technical Objective\nRetry UI\nParent Feature Link\nTP-955: Checkout recovery",
                    labels=["engineering-child", "parent-tp-955", "sync-blocked"],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "updated",
                    {
                        "updated_parent": "TP-955",
                        "updated_children": ["TP-956"],
                        "created_children": ["TP-957"],
                        "requires_input": False,
                        "parent_revision": "rev-456",
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-955")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertTrue(seed_mock.call_args.kwargs["allow_create"])

    def test_webhook_parent_run_command_is_not_consumed_by_engineering_clarification_reply(self) -> None:
        with self.session_factory() as session:
            session.add(
                FollowupContext(
                    context_id="ctx-clarify-run",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="engineering_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id=None,
                    issue_key="TP-970",
                    request_id="engineering-clarification:TP-970",
                    run_id=None,
                    metadata_json={"affected_child_keys": ["TP-971"]},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-970", labels=["pm-parent"], status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-run"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "/mb run"}]}
                ],
            },
        }

        run_plan = SimpleNamespace(content={"reason": "comment_command_run"}, actions=())
        with (
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow", return_value=run_plan) as run_flow_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-970")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        run_flow_mock.assert_called_once()
        seed_mock.assert_not_called()

    def test_webhook_parent_retry_command_is_not_consumed_by_engineering_clarification_reply(self) -> None:
        with self.session_factory() as session:
            session.add(
                FollowupContext(
                    context_id="ctx-clarify-retry",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="engineering_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id=None,
                    issue_key="TP-972",
                    request_id="engineering-clarification:TP-972",
                    run_id=None,
                    metadata_json={"affected_child_keys": ["TP-973"]},
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-972", labels=["pm-parent"], status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-retry"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "/mb retry"}]}
                ],
            },
        }

        run_plan = SimpleNamespace(content={"reason": "comment_command_retry"}, actions=())
        with (
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow", return_value=run_plan) as run_flow_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-972")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        run_flow_mock.assert_called_once()
        seed_mock.assert_not_called()
