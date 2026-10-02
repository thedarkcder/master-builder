from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.clarification.projection_service import (
    clarification_state_fingerprint,
)
from orchestrator.core.runtime.payload_models import (
    EngineeringClarification,
    PMDecisionRequest,
    StakeholderEscalation,
)
from orchestrator.core.workflow.advance import execute_workflow_advance
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    ensure_workflow_execution,
)
from orchestrator.core.workflow.operation_service import WorkflowOperationHandle
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.models import (
    FollowupContext,
    PMInterviewCase,
    Project,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from orchestrator.tools.atlassian_oauth import JiraIssueDetail
from tests.test_support.jira_webhook_harness import JiraWebhookHarness


pytestmark = pytest.mark.contract


class _JiraMetadataClientMixin:
    def update_issue_summary(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
    ) -> None:
        return None

    def replace_issue_labels(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        labels: list[str],
    ) -> None:
        return None

    def upsert_remote_issue_link(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        global_id: str,
        relationship: str,
        title: str,
        url: str,
    ) -> dict[str, object]:
        return {}

    def get_issue_detail(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> JiraIssueDetail:
        detail = self._get_issue_detail(issue_id_or_key)
        if detail.issue_type is None:
            return replace(detail, issue_type="Epic")
        return detail

    def list_project_issue_types_for_create(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
    ) -> list[str]:
        return ["Epic", "Story", "Sub-task"]

    def _get_issue_detail(self, issue_id_or_key: str) -> JiraIssueDetail:
        raise NotImplementedError

    def update_issue_fields(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
        description: str | dict[str, object],
        labels: list[str],
    ) -> None:
        self._update_issue_fields(issue_id_or_key, summary, description, labels)

    def _update_issue_fields(
        self,
        issue_id_or_key: str,
        summary: str,
        description: str | dict[str, object],
        labels: list[str],
    ) -> None:
        return None


class JiraParentClarificationFlowTests(JiraWebhookHarness):
    def _add_waiting_parent_brief_gate(
        self,
        *,
        session,
        issue_key: str,
        project_id: str = "project-1",
        display_name: str = "Identity redesign",
        description: str = "Loose parent description",
    ) -> None:
        workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
        projection = ensure_workflow_execution(
            session=session,
            workflow_type=workflow_type,
            tenant_id="tenant-webhook",
            project_id=project_id,
            execution=WorkflowExecutionReference(
                key=issue_key,
                source=WorkflowSourceReference(
                    source_system="jira",
                    source_ref=issue_key,
                    display_name=display_name,
                    description=description,
                    attributes={"jira_issue_labels": ["pm-parent", "sync-blocked"]},
                ),
            ),
            display_name=display_name,
            description=description,
        )
        operation, attempt = projection.start_operation_attempt(
            operation_type="brief_normalization"
        )
        projection.wait_started_operation(
            operation=operation,
            attempt=attempt,
            summary="PM clarification required.",
        )

    def test_webhook_comment_command_clarify_creates_parent_followup_and_blocks_child(
        self,
    ) -> None:
        payload = self._jira_issue_payload(
            issue_key="TP-960",
            labels=["engineering-child", "parent-tp-950"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-5"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "/mb clarify What should the customer see when retry succeeds?",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def __init__(self) -> None:
                self.replaced_labels: list[dict] = []

            def _get_issue_detail(self, issue_key: str):  # noqa: ANN003
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

            def replace_issue_labels(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                labels: list[str],
            ) -> None:
                self.replaced_labels.append(
                    {"issue_id_or_key": issue_id_or_key, "labels": list(labels)}
                )

        client = _FakeClient()
        oauth_context = SimpleNamespace(
            client=client,
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=object(),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.classify_engineering_clarification_with_runtime",
                return_value=EngineeringClarification(
                    classification="product_behavior",
                    stakeholder_question="When checkout retry succeeds after a recovery, what should the customer see?",
                    child_block_note="Need a PM decision on the customer-facing outcome.",
                    reason="The current brief leaves the user-facing behavior open.",
                ),
            ),
            patch(
                "orchestrator.core.integrations.atlassian.parent_child_sync_publishers.resolve_parent_feature_case",
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
            patch(
                "orchestrator.core.integrations.atlassian.parent_child_sync_publishers.resolve_platform_secret_ref",
                return_value="discord-token",
            ),
            patch(
                "orchestrator.core.integrations.atlassian.parent_child_sync_publishers.DiscordApiClient"
            ) as discord_client_cls,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment",
                return_value=(True, None),
            ) as comment_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment",
                return_value=({"id": "jira-comment-950"}, None),
            ),
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
        self.assertEqual(
            discord_client.post_message.call_args.kwargs["channel_id"],
            "discord-thread-1",
        )
        self.assertIn(
            "customer see", discord_client.post_message.call_args.kwargs["content"]
        )
        with self.session_factory() as session:
            context = session.execute(
                select(FollowupContext)
                .where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-950",
                    FollowupContext.context_type == "engineering_clarification",
                    FollowupContext.status == "active",
                )
                .order_by(FollowupContext.updated_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context.status, "active")
        with self.session_factory() as session:
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.operation_type == "jira_comment_projection",
                    WorkflowOperation.target_ref == "TP-950",
                )
            ).scalar_one()
            attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == operation.operation_id,
                )
            ).scalar_one()
        self.assertEqual(attempt.status, "completed")

    def test_webhook_parent_comment_resolves_engineering_clarification_and_closes_context(
        self,
    ) -> None:
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

        payload = self._jira_issue_payload(
            issue_key="TP-950", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-6"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Show a success banner and restore the cart summary.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def _get_issue_detail(self, issue_key: str):  # noqa: ANN003
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
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
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
            ) as seed_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment",
                return_value=(True, None),
            ) as comment_mock,
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
        seed_kwargs = seed_mock.call_args.kwargs
        self.assertIsNotNone(seed_kwargs["workflow_id"])
        self.assertIsNotNone(seed_kwargs["operation_id"])
        self.assertIsNotNone(seed_kwargs["attempt_ref"])
        self.assertIsNotNone(seed_kwargs["attempt_ref"].attempt_id)
        with self.session_factory() as session:
            operation = session.get(WorkflowOperation, seed_kwargs["operation_id"])
            assert operation is not None
            attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == operation.operation_id,
                )
            ).scalar_one()
        self.assertEqual(attempt.status, "completed")

    def test_webhook_parent_planning_clarification_reply_resumes_child_fanout_continuation(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            project = session.execute(
                select(Project).where(Project.tenant_id == "tenant-webhook")
            ).scalar_one()
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            lifecycle = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-webhook",
                project_id=project.project_id,
                execution=WorkflowExecutionReference(
                    key="TP-990",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="TP-990",
                        display_name="Identity controls",
                        description="Parent planning needs answers",
                    ),
                ),
                display_name="Identity controls",
                description="Parent planning needs answers",
            )
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id,
                    WorkflowOperation.operation_type == "backlog_planning",
                )
            ).scalar_one()
            operation.operation_id = "operation-backlog-tp-990"
            operation.status = "waiting_for_input"
            operation.target_ref = "TP-990"
            operation.summary = "Waiting for product clarification."
            operation.started_at = now
            operation.finished_at = now
            operation.updated_at = now
            session.add(
                WorkflowOperationAttempt(
                    attempt_id="attempt-backlog-tp-990",
                    operation_id=operation.operation_id,
                    attempt_number=1,
                    status="waiting_for_input",
                    started_at=now,
                    finished_at=now,
                    status_detail="Waiting for product clarification.",
                    error_category=None,
                    error_message=None,
                    retryable=True,
                    next_retry_at=None,
                    created_at=now,
                )
            )
            session.add(
                FollowupContext(
                    context_id="ctx-parent-planning-990",
                    tenant_id="tenant-webhook",
                    project_id=project.project_id,
                    context_type="parent_planning_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id="22805",
                    issue_key="TP-990",
                    request_id="parent-planning-clarification:TP-990",
                    run_id=None,
                    metadata_json={
                        "blocked_operation_type": "backlog_planning",
                        "jira_comment_id": "22805",
                        "questions": [
                            {"question": "What recovery expiry should v1 use?"}
                        ],
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-990", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": "22862",
            "parentId": "22805",
            "author": {"accountId": "jira-user-990"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Recovery grants expire after 1 hour.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def _get_issue_detail(self, issue_key: str):  # noqa: ANN003
                return JiraIssueDetail(
                    key=issue_key,
                    summary="Identity controls",
                    status="To Do",
                    description="Objective\nIdentity controls",
                    labels=["pm-parent", "sync-blocked"],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        planner_result = SimpleNamespace(
            planning_state="planning_completed", pm_decision_requests=()
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.core.parent_feature_workflow.retry_handlers.jira_child_fanout.resolve_parent_feature_brief",
                return_value=SimpleNamespace(
                    to_payload=lambda: {"objective": "Identity controls"}
                ),
            ),
            patch(
                "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                return_value=(planner_result, {"planning": "package"}),
            ) as planner_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "updated",
                    {
                        "updated_parent": "TP-990",
                        "updated_children": ["TP-991"],
                        "created_children": [],
                        "changed_children": ["TP-991"],
                        "requires_input": False,
                        "parent_revision": "rev-990",
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-990")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        planner_mock.assert_called_once()
        seed_mock.assert_called_once()
        with self.session_factory() as session:
            context = session.execute(
                select(FollowupContext).where(
                    FollowupContext.context_id == "ctx-parent-planning-990"
                )
            ).scalar_one()
            backlog_operation = session.get(
                WorkflowOperation, "operation-backlog-tp-990"
            )
            fanout_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == "parent_planning:TP-990",
                    WorkflowOperation.operation_type == "jira_child_fanout",
                )
            ).scalar_one()
            backlog_attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(
                    WorkflowOperationAttempt.operation_id == "operation-backlog-tp-990"
                )
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )
            fanout_attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(
                    WorkflowOperationAttempt.operation_id
                    == fanout_operation.operation_id
                )
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )
        self.assertEqual(context.status, "closed")
        self.assertEqual(
            context.metadata_json["answer_text"], "Recovery grants expire after 1 hour."
        )
        assert backlog_operation is not None
        self.assertEqual(backlog_operation.status, "completed")
        self.assertEqual(fanout_operation.status, "completed")
        self.assertEqual(backlog_attempts[-1].status, "completed")
        self.assertEqual(fanout_attempts[-1].status, "completed")

    def test_parent_planning_reply_keeps_context_active_when_continuation_returns_running(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            project = session.execute(
                select(Project).where(Project.tenant_id == "tenant-webhook")
            ).scalar_one()
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            lifecycle = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-webhook",
                project_id=project.project_id,
                execution=WorkflowExecutionReference(
                    key="TP-991",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="TP-991",
                        display_name="Identity controls",
                        description="Parent planning needs answers",
                    ),
                ),
                display_name="Identity controls",
                description="Parent planning needs answers",
            )
            backlog_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id,
                    WorkflowOperation.operation_type == "backlog_planning",
                )
            ).scalar_one()
            backlog_operation.status = "waiting_for_input"
            backlog_operation.target_ref = "TP-991"
            backlog_operation.summary = "Waiting for product clarification."
            backlog_operation.started_at = now
            backlog_operation.finished_at = now
            backlog_operation.updated_at = now
            fanout_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id,
                    WorkflowOperation.operation_type == "jira_child_fanout",
                )
            ).scalar_one()
            session.add(
                FollowupContext(
                    context_id="ctx-parent-planning-running-continuation",
                    tenant_id="tenant-webhook",
                    project_id=project.project_id,
                    context_type="parent_planning_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id="22805",
                    issue_key="TP-991",
                    request_id="parent-planning-clarification:TP-991",
                    run_id=None,
                    metadata_json={
                        "blocked_operation_type": "backlog_planning",
                        "jira_comment_id": "22805",
                        "questions": [
                            {"question": "What retry visibility should operators see?"}
                        ],
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()
            fanout_operation_id = fanout_operation.operation_id

        payload = self._jira_issue_payload(
            issue_key="TP-991", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": "22864",
            "parentId": "22805",
            "author": {"accountId": "jira-user-991"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Operators see redacted retry telemetry.",
                            }
                        ],
                    }
                ],
            },
        }

        with patch(
            "orchestrator.core.parent_feature_workflow.flows.retry_workflow_operation_with_registered_handler",
            return_value=WorkflowOperationHandle(
                operation_id=fanout_operation_id,
                workflow_id="parent_planning:TP-991",
                operation_type="jira_child_fanout",
                status="running",
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-991")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "failed")
        self.assertIn("reply context cannot be closed", processed.last_error or "")
        with self.session_factory() as session:
            context = session.get(
                FollowupContext, "ctx-parent-planning-running-continuation"
            )
        assert context is not None
        self.assertEqual(context.status, "active")
        self.assertEqual(
            context.metadata_json["answer_text"],
            "Operators see redacted retry telemetry.",
        )

    def test_parent_planning_reply_keeps_new_wait_context_active_after_continuation(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        original_questions = [
            {"question": "What retry visibility should operators see?"}
        ]
        next_questions = [
            {"question": "What raw telemetry retention window should apply?"}
        ]
        with self.session_factory() as session:
            project = session.execute(
                select(Project).where(Project.tenant_id == "tenant-webhook")
            ).scalar_one()
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            lifecycle = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-webhook",
                project_id=project.project_id,
                execution=WorkflowExecutionReference(
                    key="TP-992",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="TP-992",
                        display_name="Identity controls",
                        description="Parent planning needs answers",
                    ),
                ),
                display_name="Identity controls",
                description="Parent planning needs answers",
            )
            backlog_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id,
                    WorkflowOperation.operation_type == "backlog_planning",
                )
            ).scalar_one()
            backlog_operation.status = "waiting_for_input"
            backlog_operation.target_ref = "TP-992"
            backlog_operation.summary = "Waiting for product clarification."
            backlog_operation.started_at = now
            backlog_operation.finished_at = now
            backlog_operation.updated_at = now
            fanout_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id,
                    WorkflowOperation.operation_type == "jira_child_fanout",
                )
            ).scalar_one()
            session.add(
                FollowupContext(
                    context_id="ctx-parent-planning-next-wait",
                    tenant_id="tenant-webhook",
                    project_id=project.project_id,
                    context_type="parent_planning_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id="22805",
                    issue_key="TP-992",
                    request_id="parent-planning-clarification:TP-992",
                    run_id=None,
                    metadata_json={
                        "blocked_operation_type": "backlog_planning",
                        "jira_comment_id": "22805",
                        "question_state_fingerprint": clarification_state_fingerprint(
                            questions=original_questions
                        ),
                        "questions": original_questions,
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()
            fanout_operation_id = fanout_operation.operation_id

        payload = self._jira_issue_payload(
            issue_key="TP-992", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": "22865",
            "parentId": "22805",
            "author": {"accountId": "jira-user-992"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Operators see redacted retry telemetry.",
                            }
                        ],
                    }
                ],
            },
        }

        def _continue_to_new_wait(*, session, **_kwargs):  # noqa: ANN001
            context = session.get(FollowupContext, "ctx-parent-planning-next-wait")
            assert context is not None
            metadata = dict(context.metadata_json or {})
            metadata["questions"] = next_questions
            metadata["question_state_fingerprint"] = clarification_state_fingerprint(
                questions=next_questions
            )
            context.metadata_json = metadata
            return WorkflowOperationHandle(
                operation_id=fanout_operation_id,
                workflow_id="parent_planning:TP-992",
                operation_type="jira_child_fanout",
                status="waiting_for_input",
            )

        with patch(
            "orchestrator.core.parent_feature_workflow.flows.retry_workflow_operation_with_registered_handler",
            side_effect=_continue_to_new_wait,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-992")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        with self.session_factory() as session:
            context = session.get(FollowupContext, "ctx-parent-planning-next-wait")
        assert context is not None
        self.assertEqual(context.status, "active")
        self.assertEqual(context.metadata_json["questions"], next_questions)
        self.assertEqual(
            context.metadata_json["answer_text"],
            "Operators see redacted retry telemetry.",
        )

    def test_webhook_parent_engineering_clarification_without_children_fails_job(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                FollowupContext(
                    context_id="ctx-invalid-engineering-clarify",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="engineering_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id="22805",
                    issue_key="TP-991",
                    request_id="engineering-clarification:TP-991",
                    run_id=None,
                    metadata_json={
                        "source": "workflow_operation_retry",
                        "questions": [
                            {"question": "What recovery expiry should v1 use?"}
                        ],
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-991", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": "22863",
            "parentId": "22805",
            "author": {"accountId": "jira-user-991"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Recovery grants expire after 1 hour.",
                            }
                        ],
                    }
                ],
            },
        }

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
        processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-991")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "failed")
        self.assertIn("missing affected_child_keys", processed.last_error or "")

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

        payload = self._jira_issue_payload(
            issue_key="TP-955", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-6"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Yes, include manual fallback and tracking.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def _get_issue_detail(self, issue_key: str):  # noqa: ANN003
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
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
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
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment",
                return_value=(True, None),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-955")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertTrue(seed_mock.call_args.kwargs["allow_create"])

    def test_webhook_parent_comment_same_open_engineering_questions_is_idempotent(
        self,
    ) -> None:
        question_text = "Should manual fallback be included in this feature?"
        with self.session_factory() as session:
            session.add(
                FollowupContext(
                    context_id="ctx-clarify-same-state",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="engineering_clarification",
                    status="active",
                    channel_id=None,
                    thread_channel_id=None,
                    root_message_id="jira-comment-955b",
                    issue_key="TP-955B",
                    request_id="engineering-clarification:TP-955B",
                    run_id=None,
                    metadata_json={
                        "affected_child_keys": ["TP-956B"],
                        "questions": [
                            {
                                "source_child_key": "TP-956B",
                                "original_question": "Should we also track manual fallback?",
                                "stakeholder_question": question_text,
                            }
                        ],
                        "question_state_fingerprint": clarification_state_fingerprint(
                            questions=[{"question": question_text}]
                        ),
                    },
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-955B", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-6"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Yes, include manual fallback and tracking.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def _get_issue_detail(self, issue_key: str):  # noqa: ANN003
                if issue_key == "TP-955B":
                    return JiraIssueDetail(
                        key="TP-955B",
                        summary="Checkout recovery",
                        status="To Do",
                        description="Objective\nCheckout recovery parent",
                        labels=["pm-parent", "sync-blocked"],
                    )
                return JiraIssueDetail(
                    key="TP-956B",
                    summary="Retry UI behavior",
                    status="To Do",
                    description="Technical Objective\nRetry UI\nParent Feature Link\nTP-955B: Checkout recovery",
                    labels=["engineering-child", "parent-tp-955b", "sync-blocked"],
                )

            def list_issue_comments(self, **_kwargs):  # noqa: ANN003
                return [SimpleNamespace(comment_id="jira-comment-955b")]

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "blocked",
                    {
                        "updated_parent": "TP-955B",
                        "updated_children": [],
                        "created_children": [],
                        "requires_input": True,
                        "questions": [{"question": question_text}],
                        "parent_revision": "rev-955b",
                        "children_sync_status": "sync-blocked",
                    },
                ),
            ) as seed_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment",
                return_value=({"id": "jira-comment-955c"}, None),
            ) as create_comment_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment",
                return_value=(True, None),
            ) as post_comment_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-955B")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertTrue(seed_mock.called)
        create_comment_mock.assert_not_called()
        post_comment_mock.assert_not_called()

    def test_webhook_parent_pm_reply_consumes_jira_anchor_and_refreshes_children(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            self._add_waiting_parent_brief_gate(session=session, issue_key="TP-980")
            session.add(
                PMInterviewCase(
                    case_id="pm-case-980",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    request_id="pm-request-980",
                    parent_issue_key="TP-980",
                    source_kind="jira_parent",
                    status="question_pending",
                    channel_id="jira-parent-sync",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id="jira-user-980",
                    source_text="Identity redesign parent",
                    brief_json={
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "target_user": "Tenant admins",
                        "primary_journey": "From tenant settings",
                        "acceptance_criteria": ["Invitations can be sent"],
                        "scope_in": ["Tenant identity", "Invitation TTL"],
                        "scope_out": ["SSO overhaul"],
                        "ui_references": ["Admin settings"],
                        "constraints": [],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can export audit logs within policy"
                        ],
                    },
                    evidence_json=[],
                    question_history_json=[],
                    current_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    next_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    missing_slots_json=["constraints"],
                    notes_json={"source": "jira_parent_brief_normalization"},
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.add(
                FollowupContext(
                    context_id="ctx-pm-jira-980",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="pm_interview",
                    status="active",
                    channel_id="TP-980",
                    thread_channel_id=None,
                    root_message_id="jira-question-980",
                    owner_user_id="jira-user-980",
                    origin_command="pm",
                    issue_key="TP-980",
                    request_id="pm-interview-jira:TP-980",
                    run_id=None,
                    metadata_json={
                        "transport": "jira_issue_comment",
                        "reply_scope": "issue_comment_stream_from_root",
                        "pm_request_id": "pm-request-980",
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-980",
            labels=["pm-parent", "sync-blocked"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": 2002,
            "author": {"accountId": "jira-user-980"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Use a 90 day audit retention window in v1.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def _get_issue_detail(self, issue_id_or_key: str):  # noqa: ANN003
                return JiraIssueDetail(
                    key="TP-980",
                    summary="Identity redesign",
                    status="To Do",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-blocked"],
                )

            def _update_issue_fields(
                self,
                issue_id_or_key: str,
                summary: str,
                description: str | dict[str, object],
                labels: list[str],
            ) -> None:
                self.updated_fields.append(
                    {
                        "issue_id_or_key": issue_id_or_key,
                        "summary": summary,
                        "description": description,
                        "labels": list(labels),
                    }
                )

            def replace_issue_labels(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                labels: list[str],
            ) -> None:
                self.replaced_labels.append(
                    {"issue_id_or_key": issue_id_or_key, "labels": list(labels)}
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )

        def _build_local_workflow_runtime(**runtime_kwargs):  # noqa: ANN003
            class _Runtime:
                def advance(self, *, request):  # noqa: ANN003
                    workflow_type = get_workflow_type(
                        runtime_kwargs["session"], workflow_type_key="parent_planning"
                    )
                    return execute_workflow_advance(
                        session=runtime_kwargs["session"],
                        settings=runtime_kwargs["settings"],
                        workflow_type=workflow_type,
                        request=request,
                        resolve_advance_handler_fn=runtime_kwargs[
                            "resolve_advance_handler_fn"
                        ],
                    )

            return _Runtime()

        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=object(),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_workflow_runtime",
                side_effect=_build_local_workflow_runtime,
            ),
            patch(
                "orchestrator.core.pm.interview_followup_service.plan_pm_interview_with_runtime",
                return_value={
                    "message": "Retention is now clear.",
                    "brief": {
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "target_user": "Tenant admins",
                        "primary_journey": "From tenant settings",
                        "acceptance_criteria": [
                            "Invitations can be sent",
                            "Audit retention is enforced",
                        ],
                        "scope_in": ["Tenant identity", "Invitation TTL"],
                        "scope_out": ["SSO overhaul"],
                        "ui_references": ["Admin settings"],
                        "constraints": ["90 day retention window"],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can export audit logs within policy"
                        ],
                        "recommendation": "Proceed with planning.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "status": "ready_to_write",
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                return_value=(
                    SimpleNamespace(
                        planning_state="planning_completed", pm_decision_requests=()
                    ),
                    {"planning": "package"},
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "updated",
                    {
                        "updated_children": ["TP-981"],
                        "created_children": ["TP-982"],
                        "requires_input": False,
                        "parent_revision": "rev-980",
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment",
                return_value=(True, None),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-980")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_called_once()
        with self.session_factory() as session:
            followups = (
                session.execute(
                    select(FollowupContext).where(
                        FollowupContext.tenant_id == "tenant-webhook",
                        FollowupContext.issue_key == "TP-980",
                        FollowupContext.context_type == "pm_interview",
                    )
                )
                .scalars()
                .all()
            )
            case = (
                session.execute(
                    select(PMInterviewCase).where(
                        PMInterviewCase.request_id == "pm-request-980"
                    )
                )
                .scalars()
                .one()
            )
        self.assertTrue(all(row.status == "closed" for row in followups))
        self.assertEqual(case.status, "pm_completed")
        self.assertIn("90 day retention window", str(case.brief_json))
        self.assertEqual(case.brief_json["open_questions"], [])

    def test_webhook_parent_pm_reply_requires_active_jira_followup_context(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                PMInterviewCase(
                    case_id="pm-case-980-missing-followup",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    request_id="pm-request-980-missing-followup",
                    parent_issue_key="TP-980M",
                    source_kind="jira_parent",
                    status="question_pending",
                    channel_id="jira-parent-sync",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id="jira-user-980m",
                    source_text="Identity redesign parent",
                    brief_json={
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                    },
                    evidence_json=[],
                    question_history_json=[],
                    current_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    next_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    missing_slots_json=["constraints"],
                    notes_json={"source": "jira_parent_brief_normalization"},
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-980M",
            labels=["pm-parent", "sync-blocked"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": 2012,
            "author": {"accountId": "jira-user-980m"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Use a 90 day audit retention window in v1.",
                            }
                        ],
                    }
                ],
            },
        }

        with (
            patch(
                "orchestrator.core.parent_feature_workflow.flows.continue_pm_interview_from_followup"
            ) as continue_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime"
            ) as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-980M")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        continue_mock.assert_not_called()
        seed_mock.assert_not_called()

    def test_webhook_parent_pm_reply_replay_resumes_waiting_brief_gate_after_followup_closed(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-webhook",
                project_id="project-1",
                execution=WorkflowExecutionReference(
                    key="TP-980R",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="TP-980R",
                        display_name="Identity redesign",
                        description="Loose parent description",
                        attributes={"jira_issue_labels": ["pm-parent", "sync-blocked"]},
                    ),
                ),
                display_name="Identity redesign",
                description="Loose parent description",
            )
            operation, attempt = projection.start_operation_attempt(
                operation_type="brief_normalization"
            )
            projection.wait_started_operation(
                operation=operation,
                attempt=attempt,
                summary="PM clarification required.",
            )
            session.add(
                PMInterviewCase(
                    case_id="pm-case-980r",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    request_id="pm-request-980r",
                    parent_issue_key="TP-980R",
                    source_kind="jira_parent",
                    status="pm_completed",
                    channel_id="jira-parent-sync",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id="jira-user-980r",
                    source_text="Identity redesign parent",
                    brief_json={
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "target_user": "Tenant admins",
                        "acceptance_criteria": [
                            "Invitations can be sent",
                            "Audit retention is enforced",
                        ],
                        "scope_in": ["Tenant identity", "Invitation TTL"],
                        "scope_out": ["SSO overhaul"],
                        "ui_references": ["Admin settings"],
                        "constraints": ["90 day retention window"],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can export audit logs within policy"
                        ],
                        "recommendation": "Proceed with planning.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    evidence_json=[
                        {
                            "evidence_id": "2013",
                            "evidence_type": "human_reply",
                            "source_ref": "2013",
                            "summary": "jira_comment follow-up reply",
                            "content": "Use a 90 day audit retention window in v1.",
                            "metadata": {"source_transport": "jira_comment"},
                            "captured_at": now.isoformat(),
                        }
                    ],
                    question_history_json=[],
                    current_question_json={},
                    next_question_json={},
                    missing_slots_json=[],
                    notes_json={"source": "jira_pm_interview_reply"},
                    created_at=now,
                    updated_at=now,
                    closed_at=now,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-980R",
            labels=["pm-parent", "sync-blocked"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": 2013,
            "author": {"accountId": "jira-user-980r"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Use a 90 day audit retention window in v1.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def _get_issue_detail(self, issue_id_or_key: str):  # noqa: ANN003
                return JiraIssueDetail(
                    key="TP-980R",
                    summary="Identity redesign",
                    status="To Do",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-blocked"],
                )

        class _Runtime:
            def __init__(self) -> None:
                self.requests = []

            def advance(self, *, request=None, **kwargs):  # noqa: ANN003
                self.requests.append(request)
                assert request.payload["_mb_pm_interview_followup"] is True
                assert request.payload["ready_to_write"] is True
                assert "90 day retention window" in str(
                    request.payload["brief_payload"]
                )
                return SimpleNamespace(
                    handled=True, reason="pm_interview_followup_resolved", extra={}
                )

        runtime = _Runtime()
        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.core.parent_feature_workflow.flows.continue_pm_interview_from_followup"
            ) as continue_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_workflow_runtime",
                return_value=runtime,
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-980R")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        continue_mock.assert_not_called()
        self.assertEqual(len(runtime.requests), 1)

    def test_webhook_parent_pm_reply_posts_planning_blocker_after_pm_completion(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            self._add_waiting_parent_brief_gate(session=session, issue_key="TP-980B")
            session.add(
                PMInterviewCase(
                    case_id="pm-case-980b",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    request_id="pm-request-980b",
                    parent_issue_key="TP-980B",
                    source_kind="jira_parent",
                    status="question_pending",
                    channel_id="jira-parent-sync",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id="jira-user-980b",
                    source_text="Identity redesign parent",
                    brief_json={
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "target_user": "Tenant admins",
                        "acceptance_criteria": ["Invitations can be sent"],
                        "scope_in": ["Tenant identity", "Invitation TTL"],
                        "scope_out": ["SSO overhaul"],
                        "ui_references": ["Admin settings"],
                        "constraints": [],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can export audit logs within policy"
                        ],
                    },
                    evidence_json=[],
                    question_history_json=[],
                    current_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    next_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    missing_slots_json=["constraints"],
                    notes_json={"source": "jira_parent_brief_normalization"},
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.add(
                FollowupContext(
                    context_id="ctx-pm-jira-980b",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="pm_interview",
                    status="active",
                    channel_id="TP-980B",
                    thread_channel_id=None,
                    root_message_id="jira-question-980b",
                    owner_user_id="jira-user-980b",
                    origin_command="pm",
                    issue_key="TP-980B",
                    request_id="pm-interview-jira:TP-980B",
                    run_id=None,
                    metadata_json={
                        "transport": "jira_issue_comment",
                        "reply_scope": "issue_comment_stream_from_root",
                        "pm_request_id": "pm-request-980b",
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-980B",
            labels=["pm-parent", "sync-blocked"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": 2004,
            "author": {"accountId": "jira-user-980b"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Use a 90 day audit retention window in v1.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def _get_issue_detail(self, issue_id_or_key: str):  # noqa: ANN003
                return JiraIssueDetail(
                    key="TP-980B",
                    summary="Identity redesign",
                    status="To Do",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-blocked"],
                )

            def _update_issue_fields(
                self,
                issue_id_or_key: str,
                summary: str,
                description: str | dict[str, object],
                labels: list[str],
            ) -> None:
                self.updated_fields.append(
                    {
                        "issue_id_or_key": issue_id_or_key,
                        "summary": summary,
                        "description": description,
                        "labels": list(labels),
                    }
                )

            def replace_issue_labels(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                labels: list[str],
            ) -> None:
                self.replaced_labels.append(
                    {"issue_id_or_key": issue_id_or_key, "labels": list(labels)}
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )

        def _build_local_workflow_runtime(**runtime_kwargs):  # noqa: ANN003
            class _Runtime:
                def advance(self, *, request):  # noqa: ANN003
                    workflow_type = get_workflow_type(
                        runtime_kwargs["session"], workflow_type_key="parent_planning"
                    )
                    return execute_workflow_advance(
                        session=runtime_kwargs["session"],
                        settings=runtime_kwargs["settings"],
                        workflow_type=workflow_type,
                        request=request,
                        resolve_advance_handler_fn=runtime_kwargs[
                            "resolve_advance_handler_fn"
                        ],
                    )

            return _Runtime()

        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=object(),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_workflow_runtime",
                side_effect=_build_local_workflow_runtime,
            ),
            patch(
                "orchestrator.core.pm.interview_followup_service.plan_pm_interview_with_runtime",
                return_value={
                    "message": "Retention is now clear.",
                    "brief": {
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "target_user": "Tenant admins",
                        "acceptance_criteria": [
                            "Invitations can be sent",
                            "Audit retention is enforced",
                        ],
                        "scope_in": ["Tenant identity", "Invitation TTL"],
                        "scope_out": ["SSO overhaul"],
                        "ui_references": ["Admin settings"],
                        "constraints": ["90 day retention window"],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can export audit logs within policy"
                        ],
                        "recommendation": "Proceed with planning.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "status": "pm_completed",
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                return_value=(
                    SimpleNamespace(
                        planning_state="planning_blocked",
                        pm_decision_requests=(
                            PMDecisionRequest(
                                request_id="pm-entry-point",
                                question="Where should the user start this flow?",
                                why_it_matters="The answer changes the user-visible entry point.",
                                related_decision_ids=("entry-point",),
                            ),
                        ),
                    ),
                    {"planning": "package"},
                ),
            ),
            patch(
                "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.resolve_pm_decisions",
                return_value=SimpleNamespace(
                    resolved_decisions=(),
                    stakeholder_escalations=(
                        StakeholderEscalation(
                            escalation_id="stakeholder-entry-point",
                            question="Where should the user start this flow?",
                            why_it_matters="The answer changes the user-visible entry point.",
                            business_impact_area="customer_business_impact",
                            source_pm_decision_request_ids=("pm-entry-point",),
                        ),
                    ),
                    updated_planning_context={},
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "updated",
                    {
                        "updated_children": [],
                        "created_children": [],
                        "requires_input": True,
                        "questions": ["Where should the user start this flow?"],
                        "parent_revision": "rev-980b",
                        "children_sync_status": "children_syncing",
                    },
                ),
            ) as seed_mock,
            patch(
                "orchestrator.core.integrations.atlassian.parent_child_sync_publishers.resolve_platform_secret_ref",
                return_value="discord-token",
            ),
            patch(
                "orchestrator.core.integrations.atlassian.parent_child_sync_publishers.DiscordApiClient"
            ) as discord_client_cls,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment",
                return_value=({"id": "jira-question-980b-2"}, None),
            ) as create_comment_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment",
                return_value=(True, None),
            ),
        ):
            discord_client = discord_client_cls.return_value
            discord_client.post_message.return_value = {"id": "discord-msg-980b-2"}
            discord_client.create_thread_from_message.return_value = (
                "discord-thread-980b-2"
            )
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-980B")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()
        create_comment_mock.assert_called_once()

    def test_webhook_parent_pm_reply_posts_next_jira_question_when_more_detail_is_needed(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            self._add_waiting_parent_brief_gate(session=session, issue_key="TP-981")
            session.add(
                PMInterviewCase(
                    case_id="pm-case-981",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    request_id="pm-request-981",
                    parent_issue_key="TP-981",
                    source_kind="jira_parent",
                    status="question_pending",
                    channel_id="jira-parent-sync",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id="jira-user-981",
                    source_text="Identity redesign parent",
                    brief_json={
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "target_user": "Tenant admins",
                        "primary_journey": "From tenant settings",
                        "acceptance_criteria": ["Invitations can be sent"],
                        "scope_in": ["Tenant identity"],
                        "scope_out": ["SSO overhaul"],
                        "ui_references": ["Admin settings"],
                        "constraints": [],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can manage identity safely without support"
                        ],
                    },
                    evidence_json=[],
                    question_history_json=[],
                    current_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    next_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    missing_slots_json=["constraints"],
                    notes_json={"source": "jira_parent_brief_normalization"},
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.add(
                FollowupContext(
                    context_id="ctx-pm-jira-981",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="pm_interview",
                    status="active",
                    channel_id="TP-981",
                    thread_channel_id=None,
                    root_message_id="jira-question-981",
                    owner_user_id="jira-user-981",
                    origin_command="pm",
                    issue_key="TP-981",
                    request_id="pm-interview-jira:TP-981",
                    run_id=None,
                    metadata_json={
                        "transport": "jira_issue_comment",
                        "reply_scope": "issue_comment_stream_from_root",
                        "pm_request_id": "pm-request-981",
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-981",
            labels=["pm-parent", "sync-blocked"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": 2003,
            "author": {"accountId": "jira-user-981"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Retention should be configurable.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def _get_issue_detail(self, issue_id_or_key: str):  # noqa: ANN003
                return JiraIssueDetail(
                    key="TP-981",
                    summary="Identity redesign",
                    status="To Do",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-blocked"],
                )

            def _update_issue_fields(
                self,
                issue_id_or_key: str,
                summary: str,
                description: str | dict[str, object],
                labels: list[str],
            ) -> None:
                self.updated_fields.append(
                    {
                        "issue_id_or_key": issue_id_or_key,
                        "summary": summary,
                        "description": description,
                        "labels": list(labels),
                    }
                )

            def replace_issue_labels(
                self,
                *,
                access_token: str,
                cloud_id: str,
                issue_id_or_key: str,
                labels: list[str],
            ) -> None:
                self.replaced_labels.append(
                    {"issue_id_or_key": issue_id_or_key, "labels": list(labels)}
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )

        def _build_local_workflow_runtime(
            *,
            session,
            settings,
            process_claimed_run_fn,
            build_runner_fn,
            runtime_kwargs_fn,
            resolve_advance_handler_fn,
            workflow_handler_registry,
        ):  # noqa: ANN001, ANN202
            del process_claimed_run_fn, build_runner_fn, runtime_kwargs_fn, workflow_handler_registry  # fmt: skip

            class _Runtime:
                def advance(self, *, request):  # noqa: ANN001, ANN202
                    workflow_type = get_workflow_type(
                        session, workflow_type_key="parent_planning"
                    )
                    return execute_workflow_advance(
                        session=session,
                        settings=settings,
                        workflow_type=workflow_type,
                        request=request,
                        resolve_advance_handler_fn=resolve_advance_handler_fn,
                    )

            return _Runtime()

        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=object(),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_workflow_runtime",
                side_effect=_build_local_workflow_runtime,
            ),
            patch(
                "orchestrator.core.pm.interview_followup_service.plan_pm_interview_with_runtime",
                return_value={
                    "message": "I still need one more decision.",
                    "brief": {
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "target_user": "Tenant admins",
                        "primary_journey": "From tenant settings",
                        "acceptance_criteria": ["Invitations can be sent"],
                        "scope_in": ["Tenant identity"],
                        "scope_out": ["SSO overhaul"],
                        "ui_references": ["Admin settings"],
                        "constraints": [],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can manage identity safely without support"
                        ],
                        "recommendation": "Clarify recovery authority before planning.",
                        "open_questions": [
                            "What user-visible recovery option should v1 provide when a tenant admin removes a user's last valid login path?"
                        ],
                        "next_steps": [],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                    "next_question": {
                        "slot_key": "product_clarification",
                        "question": "What user-visible recovery option should v1 provide when a tenant admin removes a user's last valid login path?",
                        "examples": [
                            "Show a contact-support path only",
                            "Let another tenant admin re-invite the user",
                            "Offer a temporary recovery link for a tenant admin to approve",
                        ],
                    },
                },
            ),
            patch(
                "orchestrator.core.integrations.atlassian.parent_child_sync_publishers.resolve_platform_secret_ref",
                return_value="discord-token",
            ),
            patch(
                "orchestrator.core.integrations.atlassian.parent_child_sync_publishers.DiscordApiClient"
            ) as discord_client_cls,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment",
                return_value=({"id": "jira-question-981b"}, None),
            ) as create_comment_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment",
                return_value=(True, None),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime"
            ) as seed_mock,
        ):
            discord_client = discord_client_cls.return_value
            discord_client.post_message.return_value = {"id": "discord-msg-981"}
            discord_client.create_thread_from_message.return_value = (
                "discord-thread-981"
            )
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-981")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()
        next_question_comment = create_comment_mock.call_args.kwargs["comment"]
        self.assertIn(
            "What user-visible recovery option should v1 provide when a tenant admin removes a user's last valid login path?",
            str(next_question_comment),
        )
        with self.session_factory() as session:
            followups = (
                session.execute(
                    select(FollowupContext).where(
                        FollowupContext.tenant_id == "tenant-webhook",
                        FollowupContext.issue_key == "TP-981",
                        FollowupContext.context_type == "pm_interview",
                    )
                )
                .scalars()
                .all()
            )
            case = (
                session.execute(
                    select(PMInterviewCase).where(
                        PMInterviewCase.request_id == "pm-request-981"
                    )
                )
                .scalars()
                .one()
            )
        jira_followup = next(row for row in followups if row.channel_id == "TP-981")
        self.assertEqual(jira_followup.root_message_id, "jira-question-981b")
        self.assertEqual(case.status, "question_pending")

    def test_webhook_parent_pm_reply_commits_pm_state_before_runtime_advance(
        self,
    ) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            self._add_waiting_parent_brief_gate(session=session, issue_key="TP-981C")
            session.add(
                PMInterviewCase(
                    case_id="pm-case-981c",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    request_id="pm-request-981c",
                    parent_issue_key="TP-981C",
                    source_kind="jira_parent",
                    status="question_pending",
                    channel_id="jira-parent-sync",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id="jira-user-981c",
                    source_text="Identity redesign parent",
                    brief_json={
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "acceptance_criteria": ["Invitations can be sent"],
                        "scope_in": ["Tenant identity"],
                        "scope_out": ["SSO overhaul"],
                        "constraints": [],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can export audit logs within policy"
                        ],
                    },
                    evidence_json=[],
                    question_history_json=[],
                    current_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    next_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    missing_slots_json=["constraints"],
                    notes_json={"source": "jira_parent_brief_normalization"},
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.add(
                FollowupContext(
                    context_id="ctx-pm-jira-981c",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="pm_interview",
                    status="active",
                    channel_id="TP-981C",
                    thread_channel_id=None,
                    root_message_id="jira-question-981c",
                    owner_user_id="jira-user-981c",
                    origin_command="pm",
                    issue_key="TP-981C",
                    request_id="pm-interview-jira:TP-981C",
                    run_id=None,
                    metadata_json={
                        "transport": "jira_issue_comment",
                        "reply_scope": "issue_comment_stream_from_root",
                        "pm_request_id": "pm-request-981c",
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-981C",
            labels=["pm-parent", "sync-blocked"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": 2099,
            "author": {"accountId": "jira-user-981c"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "text",
                                "text": "Use a 12 month retention window in v1.",
                            }
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def _get_issue_detail(self, issue_id_or_key: str):  # noqa: ANN003
                return JiraIssueDetail(
                    key="TP-981C",
                    summary="Identity redesign",
                    status="To Do",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-blocked"],
                )

        session_factory = self.session_factory

        class _Runtime:
            def advance(
                self,
                *,
                session=None,
                settings=None,
                workflow_type=None,
                request=None,
            ):  # noqa: ANN003
                with session_factory() as verify_session:
                    case = verify_session.execute(
                        select(PMInterviewCase).where(
                            PMInterviewCase.request_id == "pm-request-981c"
                        )
                    ).scalar_one()
                    evidence = list(case.evidence_json or [])
                    assert len(evidence) == 1
                    assert evidence[0]["source_ref"] == "2099"
                    assert "12 month retention window" in case.brief_json["constraints"]
                return SimpleNamespace(
                    handled=True, reason="pm_interview_still_open", extra={}
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=object(),
            ),
            patch(
                "orchestrator.core.pm.interview_followup_service.plan_pm_interview_with_runtime",
                return_value={
                    "message": "I still need one more product clarification.",
                    "brief": {
                        "objective": "Tenant identity redesign",
                        "user_value": "Admins can manage identity safely",
                        "acceptance_criteria": ["Invitations can be sent"],
                        "scope_in": ["Tenant identity"],
                        "scope_out": ["SSO overhaul"],
                        "constraints": ["12 month retention window"],
                        "risks": ["Audit export misuse"],
                        "success_outcomes": [
                            "Admins can export audit logs within policy"
                        ],
                        "recommendation": "",
                        "open_questions": [
                            "What user-visible recovery option should v1 provide when a broken identity link removes a user’s only valid login path?"
                        ],
                        "next_steps": [],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                    "next_question": {
                        "slot_key": "product_clarification",
                        "question": "What user-visible recovery option should v1 provide when a broken identity link removes a user’s only valid login path?",
                        "examples": [
                            "Contact support",
                            "Tenant admin re-invite",
                            "Temporary recovery approval flow",
                        ],
                    },
                },
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_workflow_runtime",
                return_value=_Runtime(),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-981C")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

    def test_webhook_pm_interview_reply_runtime_failure_requeues_job(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                PMInterviewCase(
                    case_id="pm-case-982",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    request_id="pm-request-982",
                    parent_issue_key="TP-982",
                    source_kind="jira_parent",
                    status="question_pending",
                    channel_id="jira-parent-sync",
                    thread_channel_id=None,
                    root_message_id=None,
                    owner_user_id="jira-user-982",
                    source_text="Identity redesign parent",
                    brief_json={"objective": "Tenant identity redesign"},
                    evidence_json=[],
                    question_history_json=[],
                    current_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    next_question_json={
                        "slot_key": "constraints",
                        "question": "What audit retention window should v1 support?",
                        "examples": [],
                    },
                    missing_slots_json=["constraints"],
                    notes_json={"source": "jira_parent_brief_normalization"},
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.add(
                FollowupContext(
                    context_id="ctx-pm-jira-982",
                    tenant_id="tenant-webhook",
                    project_id="project-1",
                    context_type="pm_interview",
                    status="active",
                    channel_id="TP-982",
                    thread_channel_id=None,
                    root_message_id="jira-question-982",
                    owner_user_id="jira-user-982",
                    origin_command="pm",
                    issue_key="TP-982",
                    request_id="pm-interview-jira:TP-982",
                    run_id=None,
                    metadata_json={
                        "transport": "jira_issue_comment",
                        "reply_scope": "issue_comment_stream_from_root",
                        "pm_request_id": "pm-request-982",
                    },
                    created_at=now,
                    updated_at=now,
                    closed_at=None,
                )
            )
            session.commit()

        payload = self._jira_issue_payload(
            issue_key="TP-982",
            labels=["pm-parent", "sync-blocked"],
            status_name="To Do",
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "id": 2004,
            "author": {"accountId": "jira-user-982"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "Retention should be one year."}
                        ],
                    }
                ],
            },
        }

        class _FakeClient(_JiraMetadataClientMixin):
            def _get_issue_detail(self, issue_id_or_key: str):  # noqa: ANN003
                return JiraIssueDetail(
                    key="TP-982",
                    summary="Identity redesign",
                    status="To Do",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-blocked"],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(
                cloud_id="cloud-1", site_url="https://example.atlassian.net"
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=object(),
            ),
            patch(
                "orchestrator.core.pm.interview_followup_service.plan_pm_interview_with_runtime",
                side_effect=CodexRuntimeError(
                    "Runtime HTTP request failed: [Errno 101] Network is unreachable"
                ),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-982")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "pending")
        self.assertIn("Network is unreachable", str(processed.last_error))

    def test_webhook_parent_run_command_is_not_consumed_by_engineering_clarification_reply(
        self,
    ) -> None:
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

        payload = self._jira_issue_payload(
            issue_key="TP-970", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-run"},
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

        run_plan = SimpleNamespace(
            content={"reason": "comment_command_run"}, actions=()
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_application.plan_jira_run_flow",
                return_value=run_plan,
            ) as run_flow_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime"
            ) as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-970")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        run_flow_mock.assert_called_once()
        seed_mock.assert_not_called()

    def test_webhook_parent_retry_command_is_not_consumed_by_engineering_clarification_reply(
        self,
    ) -> None:
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

        payload = self._jira_issue_payload(
            issue_key="TP-972", labels=["pm-parent"], status_name="To Do"
        )
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-retry"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "/mb retry"}],
                    }
                ],
            },
        }

        run_plan = SimpleNamespace(
            content={"reason": "comment_command_retry"}, actions=()
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_application.plan_jira_run_flow",
                return_value=run_plan,
            ) as run_flow_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime"
            ) as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-972")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        run_flow_mock.assert_called_once()
        seed_mock.assert_not_called()
