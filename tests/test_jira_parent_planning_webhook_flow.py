from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from orchestrator.api.webhooks.jira_application import JiraWebhookPlan
from orchestrator.api.discord.seed.description import build_parent_feature_description
from orchestrator.core.followup_context_service import FOLLOWUP_CONTEXT_PM_INTERVIEW, upsert_followup_context
from orchestrator.core.pm_interview_service import PM_INTERVIEW_STATUS_QUESTION_PENDING
from orchestrator.core.parent_feature_brief_store import persist_parent_feature_brief_snapshot
from orchestrator.storage.models import (
    FollowupContext,
    PMInterviewCase,
    Project,
    Tenant,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from orchestrator.tools.jira_oauth import JiraIssueDetail, JiraIssuePreview
from tests.test_support.jira_webhook_api_harness import JiraWebhookTestsHarness


pytestmark = pytest.mark.contract


class JiraParentPlanningWebhookFlowTests(JiraWebhookTestsHarness):
    def test_webhook_unlabeled_backlog_issue_created_auto_routes_pm_parent_and_seeds_engineering_children(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-990", labels=[], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        class _FakeClient:
            def __init__(self) -> None:
                self.labels: list[str] = []
                self.updated_fields: list[dict] = []
                self.added_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-990",
                    summary="Runtime routing reset",
                    status="Backlog",
                    description="Need the system to route new Jira issues automatically and create child tickets when needed.",
                    labels=list(self.labels),
                )

            def add_issue_labels(self, **kwargs):
                self.added_labels.append(kwargs)
                for label in kwargs.get("labels", []):
                    if label not in self.labels:
                        self.labels.append(label)
                return None

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_application.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_application.classify_jira_issue_intake_with_runtime",
                return_value={"route": "pm_parent", "reason": "Needs PM breakdown", "confidence": "high"},
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Automatically route new Jira issues",
                        "user_value": "Users can drop issues into backlog without remembering special labels.",
                        "acceptance_criteria": ["Parent issues create engineering child tickets automatically."],
                        "scope_in": ["Backlog intake routing"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Route as a PM parent and seed children.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": [],
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.run_specialist_planning_fanout",
                return_value=SimpleNamespace(
                    planning_state="planning_completed",
                    required_tasks=("Create child tickets automatically",),
                    findings=("Issue needs PM decomposition before execution.",),
                    recommendations=("Seed engineering child tickets from the parent brief.",),
                    acceptance_impacts=("Parent planning should happen automatically in backlog.",),
                    open_behavior_questions=(),
                    architecture_summary=("Seed engineering child tickets from the parent brief.",),
                    architecture_diagram="",
                    stages=(
                        SimpleNamespace(
                            planning_state="engineering_planning",
                            persona_id="architect",
                            to_payload=lambda: {
                                "findings": ["Issue needs PM decomposition before execution."],
                                "recommendations": ["Seed engineering child tickets from the parent brief."],
                                "required_tasks": ["Create child tickets automatically"],
                                "child_ticket_specs": [
                                    {
                                        "summary": "Create child tickets automatically",
                                        "capability": "Automatic child-ticket fanout",
                                        "delivery": "Build parent planning fanout so backlog parent issues create the required engineering child tickets automatically.",
                                        "expected_outcome": "A PM parent in backlog produces executable engineering child tickets without manual label work.",
                                        "acceptance_criteria": ["Backlog parent issues create engineering child tickets automatically"],
                                        "how_to_test": ["Trigger parent planning for a backlog issue and verify child ticket creation"],
                                        "done_means": ["Parent planning creates the expected engineering child tickets"],
                                        "dependencies": [],
                                        "risks": ["Duplicate child creation if fanout is not idempotent"],
                                        "labels": ["engineering"],
                                    }
                                ],
                                "open_behavior_questions": [],
                                "acceptance_impacts": ["Parent planning should happen automatically in backlog."],
                            },
                        ),
                    ),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "seeded",
                    {
                        "updated_parent": "TP-990",
                        "updated_children": [],
                        "created_children": ["TP-991"],
                        "requires_input": False,
                        "parent_revision": "rev-990",
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-990")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(oauth_context.client.labels, ["pm-parent"])
        seed_mock.assert_called_once()
        run_flow_mock.assert_not_called()

    def test_webhook_unlabeled_backlog_issue_created_auto_routes_engineering_child(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-992", labels=[], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        class _FakeClient:
            def __init__(self) -> None:
                self.labels: list[str] = []
                self.added_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-992",
                    summary="Fix the queue selector duplicate scan ordering",
                    status="Backlog",
                    description="Tight implementation slice for queue selection ordering.",
                    labels=list(self.labels),
                )

            def add_issue_labels(self, **kwargs):
                self.added_labels.append(kwargs)
                for label in kwargs.get("labels", []):
                    if label not in self.labels:
                        self.labels.append(label)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_application.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_application.classify_jira_issue_intake_with_runtime",
                return_value={"route": "engineering_child", "reason": "Already implementation scoped", "confidence": "high"},
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
            patch(
                "orchestrator.api.webhooks.jira_application.plan_jira_run_flow",
                return_value=JiraWebhookPlan(content={"reason": "issue_in_backlog"}),
            ) as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-992")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(oauth_context.client.labels, ["engineering-child"])
        seed_mock.assert_not_called()
        run_flow_mock.assert_called_once()

    def test_webhook_requeues_when_intake_routing_classification_fails(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-992B", labels=[], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        class _FakeClient:
            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-992B",
                    summary="Runtime routing reset",
                    status="Backlog",
                    description="Need runtime classification before deciding PM-parent handling.",
                    labels=[],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_application.classify_jira_issue_intake_with_runtime",
                side_effect=RuntimeError("runtime returned invalid json"),
            ),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-992B")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "pending")
        run_flow_mock.assert_not_called()

        with self.session_factory() as session:
            persisted = session.get(type(processed), processed.job_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "pending")
            self.assertEqual(persisted.attempt_count, 1)
            self.assertEqual(persisted.last_error, "Jira issue intake routing classification failed")

    def test_webhook_requeues_when_intake_routing_label_update_fails(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-992C", labels=[], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        class _FailingClient:
            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-992C",
                    summary="Runtime routing reset",
                    status="Backlog",
                    description="Need runtime classification before deciding PM-parent handling.",
                    labels=[],
                )

            def add_issue_labels(self, **kwargs):
                raise RuntimeError("jira temporarily unavailable")

        oauth_context = SimpleNamespace(
            client=_FailingClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_application.classify_jira_issue_intake_with_runtime",
                return_value={"route": "pm_parent", "reason": "Needs PM breakdown", "confidence": "high"},
            ),
            patch("orchestrator.api.webhooks.jira_application.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-992C")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "pending")
        run_flow_mock.assert_not_called()

        with self.session_factory() as session:
            persisted = session.get(type(processed), processed.job_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "pending")
            self.assertEqual(persisted.attempt_count, 1)
            self.assertEqual(persisted.last_error, "Jira issue intake routing label update failed")

    def test_webhook_unlabeled_todo_issue_in_backlog_auto_routes_pm_parent(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-992A", labels=[], status_name="To Do")
        payload["webhookEvent"] = "jira:issue_updated"

        class _FakeClient:
            def __init__(self) -> None:
                self.labels: list[str] = []
                self.added_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-992A",
                    summary="Runtime routing reset",
                    status="To Do",
                    description="Need the system to route backlog issues even when the workflow status remains To Do.",
                    labels=list(self.labels),
                )

            def add_issue_labels(self, **kwargs):
                self.added_labels.append(kwargs)
                for label in kwargs.get("labels", []):
                    if label not in self.labels:
                        self.labels.append(label)
                return None

            def update_issue_fields(self, **kwargs):
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_application.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_application.resolve_project_issue_board_location",
                return_value=("backlog", None),
            ),
            patch(
                "orchestrator.api.webhooks.jira_application.classify_jira_issue_intake_with_runtime",
                return_value={"route": "pm_parent", "reason": "Still a parent feature in backlog", "confidence": "high"},
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Automatically route backlog issues",
                        "user_value": "PM-parent routing still works from board backlog placement.",
                        "acceptance_criteria": ["Backlog board placement is treated as PM intake eligible."],
                        "scope_in": ["Backlog intake routing"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Route as a PM parent and seed children.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": [],
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.run_specialist_planning_fanout",
                return_value=SimpleNamespace(
                    planning_state="planning_completed",
                    required_tasks=("Seed child tickets from backlog issue",),
                    findings=("Backlog board placement should drive intake routing.",),
                    recommendations=("Reuse board-location checks for PM routing.",),
                    acceptance_impacts=("Board backlog issues still seed children.",),
                    open_behavior_questions=(),
                    architecture_summary=("Reuse board-location checks for PM routing.",),
                    architecture_diagram="",
                    stages=(
                        SimpleNamespace(
                            planning_state="engineering_planning",
                            persona_id="architect",
                            to_payload=lambda: {
                                "findings": ["Backlog board placement should drive intake routing."],
                                "recommendations": ["Reuse board-location checks for PM routing."],
                                "required_tasks": ["Seed child tickets from backlog issue"],
                                "child_ticket_specs": [
                                    {
                                        "summary": "Seed child tickets from backlog issue",
                                        "capability": "Backlog intake routing",
                                        "delivery": "Build backlog intake routing so a backlog parent issue triggers engineering child ticket seeding.",
                                        "expected_outcome": "Backlog board placement is enough to start parent planning and child seeding.",
                                        "acceptance_criteria": ["Board backlog issues still seed children"],
                                        "how_to_test": ["Move a parent issue into backlog and verify engineering child ticket seeding"],
                                        "done_means": ["Backlog board placement triggers child seeding deterministically"],
                                        "dependencies": [],
                                        "risks": ["Board rules may diverge across projects"],
                                        "labels": ["engineering"],
                                    }
                                ],
                                "open_behavior_questions": [],
                                "acceptance_impacts": ["Board backlog issues still seed children."],
                            },
                        ),
                    ),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "seeded",
                    {
                        "updated_parent": "TP-992A",
                        "updated_children": [],
                        "created_children": ["TP-992B"],
                        "requires_input": False,
                        "parent_revision": "rev-992A",
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-992A")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(oauth_context.client.labels, ["pm-parent"])
        seed_mock.assert_called_once()
        run_flow_mock.assert_not_called()

    def test_webhook_pm_parent_material_change_refreshes_engineering_children(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-950", labels=["pm-parent"], status_name="To Do")
        payload["changelog"] = {"items": [{"field": "description", "fromString": "old", "toString": "new"}]}

        class _FakeClient:
            def search_issues_by_jql(self, **kwargs):
                jql = kwargs["jql"]
                if 'parent = "TP-950"' in jql or 'labels = "parent-tp-950"' in jql:
                    return [JiraIssuePreview(key="TP-951", summary="Update retry UI", status="To Do")]
                return []

            def update_issue_fields(self, **kwargs):
                return None

            def get_issue_detail(self, **kwargs):
                issue_key = kwargs["issue_id_or_key"]
                if issue_key == "TP-950":
                    return JiraIssueDetail(
                        key="TP-950",
                        summary="Checkout recovery",
                        status="To Do",
                        description="Objective\nRefresh checkout recovery behavior\nOpen Questions\nNo open questions remain",
                        labels=["pm-parent", "sync-current"],
                    )
                return JiraIssueDetail(
                    key="TP-951",
                    summary="Update retry UI",
                    status="To Do",
                    description="Technical Objective\nRefresh retry UI\nParent Feature Link\nTP-950: Checkout recovery\nBehavior Slice\nRetry success messaging",
                    labels=["engineering-child", "parent-tp-950", "sync-current"],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        normalized_brief = {
            "objective": "Refresh checkout recovery behavior",
            "user_value": "Customers recover checkout after a failed attempt.",
            "acceptance_criteria": ["Users can retry checkout successfully after a transient failure."],
        }
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=SimpleNamespace(slug="pm-normalization-runtime"),
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={"brief": normalized_brief, "open_questions": []},
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "synced",
                    {
                        "updated_parent": "TP-950",
                        "updated_children": ["TP-951"],
                        "created_children": [],
                        "requires_input": False,
                        "parent_revision": "rev-123",
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-950")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_called_once()
        self.assertTrue(seed_mock.call_args.kwargs["allow_create"])
        run_flow_mock.assert_not_called()
        self.assertGreaterEqual(comment_mock.call_count, 2)

    def test_webhook_pm_parent_material_change_with_no_child_delta_is_successful_no_op(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-953", labels=["pm-parent"], status_name="To Do")
        payload["changelog"] = {"items": [{"field": "description", "fromString": "old", "toString": "new"}]}

        class _FakeClient:
            def search_issues_by_jql(self, **kwargs):
                jql = kwargs["jql"]
                if 'parent = "TP-953"' in jql or 'labels = "parent-tp-953"' in jql:
                    return [JiraIssuePreview(key="TP-954", summary="Refresh retry UI", status="To Do")]
                return []

            def update_issue_fields(self, **kwargs):
                return None

            def get_issue_detail(self, **kwargs):
                issue_key = kwargs["issue_id_or_key"]
                if issue_key == "TP-953":
                    return JiraIssueDetail(
                        key="TP-953",
                        summary="Checkout recovery",
                        status="To Do",
                        description="Objective\nRefresh checkout recovery behavior",
                        labels=["pm-parent", "sync-current"],
                    )
                return JiraIssueDetail(
                    key="TP-954",
                    summary="Refresh retry UI",
                    status="To Do",
                    description="Technical Objective\nRefresh retry UI\nParent Feature Link\nTP-953: Checkout recovery",
                    labels=["engineering-child", "parent-tp-953", "sync-current"],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        normalized_brief = {
            "objective": "Refresh checkout recovery behavior",
            "user_value": "Customers recover checkout after a failed attempt.",
            "acceptance_criteria": ["Users can retry checkout successfully after a transient failure."],
        }
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector",
                return_value=SimpleNamespace(slug="pm-normalization-runtime"),
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={"brief": normalized_brief, "open_questions": []},
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "synced",
                    {
                        "updated_parent": "TP-953",
                        "updated_children": [],
                        "created_children": [],
                        "requires_input": False,
                        "parent_revision": "rev-123",
                        "children_sync_status": "children_current",
                    },
                ),
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-953")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        parent_sync_comment = comment_mock.call_args_list[0].kwargs["comment"]
        self.assertIn("No engineering child changes were required.", parent_sync_comment)

    def test_webhook_pm_parent_non_material_change_skips_child_sync(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-952", labels=["pm-parent"], status_name="To Do")
        payload["changelog"] = {"items": [{"field": "status", "fromString": "To Do", "toString": "In Progress"}]}

        class _FakeClient:
            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key=str(kwargs["issue_id_or_key"]),
                    summary="Parent feature",
                    status="In Progress",
                    description="Objective\nParent feature description",
                    labels=["pm-parent", "sync-current"],
                )

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-952")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()

    def test_webhook_pm_parent_transition_to_todo_promotes_backlog_engineering_children(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-980", labels=["pm-parent"], status_name="To Do")
        payload["changelog"] = {"items": [{"field": "status", "fromString": "Backlog", "toString": "To Do"}]}

        class _FakeClient:
            def __init__(self) -> None:
                self.transitions: list[dict[str, str]] = []

            def search_issues_by_jql(self, **kwargs):
                jql = kwargs["jql"]
                if 'parent = "TP-980"' in jql or 'labels = "parent-tp-980"' in jql:
                    return [
                        JiraIssuePreview(key="TP-981", summary="First child", status="Backlog"),
                        JiraIssuePreview(key="TP-982", summary="Already on board", status="To Do"),
                    ]
                return []

            def get_issue_detail(self, **kwargs):
                issue_key = kwargs["issue_id_or_key"]
                if issue_key == "TP-980":
                    return JiraIssueDetail(
                        key="TP-980",
                        summary="Parent feature",
                        status="To Do",
                        description="Objective\nParent feature description",
                        labels=["pm-parent", "sync-current"],
                    )
                if issue_key == "TP-981":
                    return JiraIssueDetail(
                        key="TP-981",
                        summary="First child",
                        status="Backlog",
                        description="Technical Objective\nFirst child behavior",
                        labels=["engineering-child", "parent-tp-980", "sync-current"],
                    )
                return JiraIssueDetail(
                    key="TP-982",
                    summary="Already on board",
                    status="To Do",
                    description="Technical Objective\nSecond child behavior",
                    labels=["engineering-child", "parent-tp-980", "sync-current"],
                )

            def transition_issue(self, **kwargs):
                self.transitions.append(
                    {
                        "issue_id_or_key": str(kwargs["issue_id_or_key"]),
                        "target_status": str(kwargs["target_status"]),
                    }
                )
                return {"to_status": str(kwargs["target_status"])}

        fake_client = _FakeClient()
        oauth_context = SimpleNamespace(
            client=fake_client,
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-980")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()
        run_flow_mock.assert_not_called()
        self.assertEqual(
            fake_client.transitions,
            [{"issue_id_or_key": "TP-981", "target_status": "To Do"}],
        )
        parent_comment = comment_mock.call_args_list[0].kwargs["comment"]
        self.assertIn("Promoted engineering child tickets to To Do: TP-981.", parent_comment)
        self.assertIn("Already on board or terminal: TP-982.", parent_comment)

    def test_webhook_pm_parent_issue_created_in_backlog_seeds_engineering_children(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-983", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-983",
                    summary="Runtime architecture reset",
                    status="Backlog",
                    description="Objective\nRefactor the orchestration stack\nAcceptance Criteria\nCreate the engineering child tickets.",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Runtime architecture reset",
                        "user_value": "Orchestration behavior is easier to evolve and verify",
                        "acceptance_criteria": ["Create the engineering child tickets."],
                        "scope_in": ["Runtime architecture reset"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Seed the engineering child tickets from the normalized brief.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": [],
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.run_specialist_planning_fanout",
                return_value=SimpleNamespace(
                    planning_state="planning_completed",
                    required_tasks=("Create runtime-agnostic issue seeding interface", "Add architect Mermaid output"),
                    findings=("Seed/runtime boundary is implementation-specific.",),
                    recommendations=("Promote runtime selection into the adapter layer.",),
                    acceptance_impacts=("Parent issue should include architecture context.",),
                    open_behavior_questions=(),
                    architecture_summary=(
                        "Promote runtime selection into the adapter layer.",
                        "Parent issue should include architecture context.",
                    ),
                    architecture_diagram="flowchart TD\n  Parent[Parent issue] --> Planner[Planning runtime]",
                    stages=(
                        SimpleNamespace(
                            planning_state="engineering_planning",
                            persona_id="architect",
                            to_payload=lambda: {
                                "findings": ["Seed/runtime boundary is implementation-specific."],
                                "recommendations": ["Promote runtime selection into the adapter layer."],
                                "required_tasks": ["Create runtime-agnostic issue seeding interface"],
                                "child_ticket_specs": [
                                    {
                                        "summary": "Create runtime-agnostic issue seeding interface",
                                        "capability": "Issue seeding boundary",
                                        "delivery": "Build a runtime-agnostic issue seeding interface so planning can create Jira children without knowing the concrete runtime implementation.",
                                        "expected_outcome": "Issue seeding works through a stable adapter boundary instead of runtime-specific wiring.",
                                        "acceptance_criteria": ["Parent issue includes architecture context", "Issue seeding is routed through the adapter boundary"],
                                        "how_to_test": ["Run parent backlog planning and verify child seeding through the adapter path"],
                                        "done_means": ["Issue seeding is runtime-agnostic and verified"],
                                        "dependencies": [],
                                        "risks": ["Runtime-specific logic could leak back into the planning path"],
                                        "labels": ["engineering"],
                                    }
                                ],
                                "open_behavior_questions": [],
                                "acceptance_impacts": ["Parent issue should include architecture context."],
                                "mermaid_diagram": "flowchart TD\n  Parent[Parent issue] --> Planner[Planning runtime]",
                            },
                        ),
                        SimpleNamespace(
                            planning_state="security_planning",
                            persona_id="security",
                            to_payload=lambda: {
                                "findings": ["No additional security blockers."],
                                "recommendations": ["Keep Jira payloads free of secrets."],
                                "required_tasks": [],
                                "open_behavior_questions": [],
                                "acceptance_impacts": ["Backlog planning remains safe to run automatically."],
                            },
                        ),
                        SimpleNamespace(
                            planning_state="test_planning",
                            persona_id="qa",
                            to_payload=lambda: {
                                "findings": ["Regression coverage is required."],
                                "recommendations": ["Cover parent backlog planning and fanout."],
                                "required_tasks": ["Add architect Mermaid output"],
                                "open_behavior_questions": [],
                                "acceptance_impacts": ["Parent planning remains deterministic."],
                            },
                        ),
                    ),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "seeded",
                    {
                        "updated_parent": "TP-983",
                        "updated_children": [],
                        "created_children": ["TP-984", "TP-985"],
                        "requires_input": False,
                        "parent_revision": "rev-789",
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-983")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_called_once()
        self.assertEqual(seed_mock.call_args.kwargs["force_issue_keys"], ["TP-983"])
        self.assertTrue(seed_mock.call_args.kwargs["allow_create"])
        self.assertEqual(seed_mock.call_args.kwargs["planning_package"]["planning_state"], "planning_completed")
        self.assertIn("architecture", seed_mock.call_args.kwargs["planning_package"]["specialist_outputs"])
        self.assertIn(
            "Parent[Parent issue] --> Planner[Planning runtime]",
            seed_mock.call_args.kwargs["planning_package"]["architecture_diagram"],
        )
        with self.session_factory() as session:
            snapshot = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "parent-brief:TP-983")
            ).scalars().one()
        self.assertEqual(snapshot.parent_issue_key, "TP-983")
        self.assertEqual(snapshot.brief_json["objective"], "Runtime architecture reset")
        self.assertEqual(snapshot.notes_json["source"], "jira_parent_brief_normalization")
        with self.session_factory() as session:
            workflow = session.get(WorkflowExecution, "parent_planning:TP-983")
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "completed")
            operations = session.execute(
                select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
            ).scalars().all()
        status_by_type = {operation.operation_type: operation.status for operation in operations}
        self.assertEqual(
            status_by_type,
            {
                "brief_normalization": "completed",
                "jira_parent_update": "completed",
                "jira_comment_projection": "pending",
                "discord_followup_projection": "pending",
                "backlog_planning": "completed",
                "jira_child_fanout": "completed",
                "jira_child_promotion": "pending",
                "notification_emit": "pending",
            },
        )
        self.assertTrue(oauth_context.client.updated_fields)
        rewritten_description = oauth_context.client.updated_fields[0]["description"]
        self.assertIn("Objective", str(rewritten_description))
        self.assertIn("Architecture Context", str(rewritten_description))
        run_flow_mock.assert_not_called()
        parent_comment = comment_mock.call_args_list[0].kwargs["comment"]
        self.assertIn("Created engineering child tickets: TP-984, TP-985.", parent_comment)

    def test_webhook_pm_parent_issue_created_uses_canonical_parent_brief_snapshot(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-986", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            project = session.execute(select(Project).where(Project.tenant_id == "tenant-webhook")).scalars().first()
            self.assertIsNotNone(tenant)
            self.assertIsNotNone(project)
            persist_parent_feature_brief_snapshot(
                session=session,
                tenant_id="tenant-webhook",
                project_id=getattr(project, "project_id", None),
                parent_issue_key="TP-986",
                source_text="Legacy parent description",
                brief={
                    "objective": "Canonical parent objective",
                    "user_value": "Canonical user value",
                    "acceptance_criteria": ["Canonical acceptance criteria"],
                },
                notes={"source": "test"},
            )
            session.commit()

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-986",
                    summary="Runtime architecture reset",
                    status="Backlog",
                    description="Objective\nDescription objective should not be used\nAcceptance Criteria\nDescription acceptance should not be used.",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch("orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime") as normalize_mock,
            patch(
                "orchestrator.core.jira_parent_child_sync_service.run_specialist_planning_fanout",
                return_value=SimpleNamespace(
                    planning_state="planning_completed",
                    required_tasks=(),
                    findings=(),
                    recommendations=(),
                    acceptance_impacts=(),
                    open_behavior_questions=(),
                    architecture_summary=(),
                    architecture_diagram=None,
                    stages=(),
                ),
            ) as planning_mock,
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "seeded",
                    {
                        "updated_parent": "TP-986",
                        "updated_children": [],
                        "created_children": [],
                        "requires_input": False,
                        "parent_revision": "rev-790",
                        "children_sync_status": "children_current",
                    },
                ),
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-986")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        planning_request = planning_mock.call_args.kwargs["request"]
        self.assertEqual(planning_request.product_brief["objective"], "Canonical parent objective")
        self.assertEqual(planning_request.product_brief["user_value"], "Canonical user value")
        self.assertEqual(planning_request.product_brief["acceptance_criteria"], ["Canonical acceptance criteria"])
        self.assertTrue(oauth_context.client.updated_fields)
        normalize_mock.assert_not_called()
        run_flow_mock.assert_not_called()

    def test_webhook_pm_parent_issue_created_blocked_questions_post_back_to_discord(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-987", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"
        payload["issue"]["fields"]["reporter"] = {"accountId": "jira-user-987", "displayName": "Casey Reporter"}

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-987",
                    summary="Runtime architecture reset",
                    status="Backlog",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        discord_client = MagicMock()
        discord_client.post_message.return_value = {"id": "discord-msg-987"}
        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Runtime architecture reset",
                        "user_value": "Orchestration behavior is easier to evolve",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Architecture reset"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Clarify the release-train ownership model before planning.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": ["Who owns release-train supervision: the parent PM flow or one child ticket?"],
                    "ready_to_write": False,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_publishers.resolve_parent_feature_case",
                return_value=SimpleNamespace(
                    request_id="pm-request-987",
                    channel_id="discord-channel-1",
                    thread_channel_id="discord-thread-1",
                    root_message_id="root-msg-1",
                    owner_user_id="discord-user-1",
                ),
            ),
            patch("orchestrator.core.jira_parent_child_sync_publishers.resolve_platform_secret_ref", return_value="discord-bot-token"),
            patch("orchestrator.core.jira_parent_child_sync_publishers.DiscordApiClient", return_value=discord_client),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment", return_value=({"id": "jira-comment-987"}, None)) as create_comment_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()
        run_flow_mock.assert_not_called()
        discord_client.post_message.assert_called_once()
        discord_message = discord_client.post_message.call_args.kwargs["content"]
        self.assertIn("TP-987", discord_message)
        self.assertIn("Who owns release-train supervision", discord_message)
        self.assertTrue(oauth_context.client.updated_fields)
        self.assertEqual(len(oauth_context.client.replaced_labels), 1)
        self.assertGreaterEqual(create_comment_mock.call_count, 1)
        jira_question_comment = create_comment_mock.call_args_list[0].kwargs["comment"]
        self.assertEqual(jira_question_comment["type"], "doc")
        intro_content = jira_question_comment["content"][0]["content"]
        self.assertEqual(intro_content[0]["type"], "text")
        self.assertTrue(intro_content[0]["text"].startswith("[mb-system]"))
        self.assertEqual(intro_content[1]["type"], "mention")
        self.assertEqual(intro_content[1]["attrs"]["id"], "jira-user-987")
        ordered_questions = jira_question_comment["content"][1]["content"]
        self.assertEqual(len(ordered_questions), 1)
        self.assertIn("Who owns release-train supervision", ordered_questions[0]["content"][0]["content"][0]["text"])
        with self.session_factory() as session:
            followups = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-987",
                    FollowupContext.context_type == "pm_interview",
                )
            ).scalars().all()
        self.assertEqual(len(followups), 2)
        discord_followup = next(row for row in followups if row.thread_channel_id == "discord-thread-1")
        jira_followup = next(row for row in followups if row.channel_id == "TP-987")
        self.assertEqual(discord_followup.request_id, "pm-request-987")
        self.assertEqual(discord_followup.status, "active")
        self.assertEqual(jira_followup.root_message_id, "jira-comment-987")
        self.assertEqual(jira_followup.metadata_json.get("transport"), "jira_issue_comment")
        self.assertEqual(jira_followup.metadata_json.get("pm_request_id"), "pm-request-987")
        with self.session_factory() as session:
            snapshot = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "parent-brief:TP-987")
            ).scalars().one()
            workflow = session.get(WorkflowExecution, "parent_planning:TP-987")
            self.assertIsNotNone(workflow)
            assert workflow is not None
            operations = session.execute(
                select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
            ).scalars().all()
        self.assertEqual(snapshot.status, PM_INTERVIEW_STATUS_QUESTION_PENDING)
        self.assertEqual(workflow.status, "waiting_for_input")
        status_by_type = {operation.operation_type: operation.status for operation in operations}
        self.assertEqual(status_by_type["brief_normalization"], "waiting_for_input")
        self.assertEqual(status_by_type["jira_parent_update"], "completed")
        self.assertEqual(status_by_type["jira_comment_projection"], "completed")
        self.assertEqual(status_by_type["discord_followup_projection"], "completed")

    def test_webhook_pm_parent_issue_created_fails_when_discord_followup_projection_fails(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-987A", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"
        payload["issue"]["fields"]["reporter"] = {"accountId": "jira-user-987A", "displayName": "Casey Reporter"}

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-987A",
                    summary="Runtime architecture reset",
                    status="Backlog",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Runtime architecture reset",
                        "user_value": "Orchestration behavior is easier to evolve",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Architecture reset"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Clarify who owns release-train supervision.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": ["Who owns release-train supervision: the parent PM flow or one child ticket?"],
                    "ready_to_write": False,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_publishers.resolve_parent_feature_case",
                return_value=SimpleNamespace(
                    request_id="pm-request-987A",
                    channel_id="discord-channel-1",
                    thread_channel_id="discord-thread-1",
                    root_message_id="root-msg-1",
                    owner_user_id="discord-user-1",
                ),
            ),
            patch("orchestrator.core.jira_parent_child_sync_publishers.resolve_platform_secret_ref", return_value=None),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment", return_value=({"id": "jira-comment-987A"}, None)) as create_comment_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987A")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "failed")
        seed_mock.assert_not_called()
        run_flow_mock.assert_not_called()
        self.assertTrue(oauth_context.client.updated_fields)
        self.assertEqual(len(oauth_context.client.replaced_labels), 1)
        create_comment_mock.assert_not_called()
        with self.session_factory() as session:
            followups = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-987A",
                    FollowupContext.context_type == "pm_interview",
                )
            ).scalars().all()
            snapshot = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "parent-brief:TP-987A")
            ).scalars().first()
        self.assertEqual(followups, [])
        self.assertIsNone(snapshot)

    def test_webhook_pm_parent_issue_created_planning_block_fails_without_discord_projection(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-987C", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"
        payload["issue"]["fields"]["reporter"] = {"accountId": "jira-user-987C", "displayName": "Casey Reporter"}

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-987C",
                    summary="Runtime architecture reset",
                    status="Backlog",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Runtime architecture reset",
                        "user_value": "Orchestration behavior is easier to evolve",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Architecture reset"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Clarify ownership before fanout.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": [],
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.run_specialist_planning_fanout",
                return_value=SimpleNamespace(
                    planning_state="planning_needs_clarification",
                    required_tasks=(),
                    findings=(),
                    recommendations=(),
                    acceptance_impacts=(),
                    open_behavior_questions=(
                        {
                            "id": "Q-1",
                            "question": "What is the required user-visible behavior when a broken identity link is detected for a still-active sensitive session?",
                            "why_it_matters": "This defines the recovery and assurance contract.",
                        },
                        {
                            "id": "Q-2",
                            "question": "What cooldown or rate-limit behavior should users see on repeated auth-initiation attempts?",
                        },
                    ),
                    architecture_summary=(),
                    architecture_diagram="",
                    stages=(),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                return_value=(
                    "blocked",
                    {
                        "updated_parent": "TP-987C",
                        "updated_children": [],
                        "created_children": [],
                        "requires_input": True,
                        "questions": [],
                        "parent_revision": "rev-987C",
                        "children_sync_status": "planning_blocked",
                    },
                ),
            ) as seed_mock,
            patch("orchestrator.core.jira_parent_child_sync_publishers.resolve_platform_secret_ref", return_value=None),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment", return_value=({"id": "jira-comment-987C"}, None)) as create_comment_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987C")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "failed")
        seed_mock.assert_called_once()
        run_flow_mock.assert_not_called()
        create_comment_mock.assert_not_called()

    def test_webhook_pm_parent_issue_created_seed_failure_records_failed_execution_operation(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-987D", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-987D",
                    summary="Identity redesign",
                    status="Backlog",
                    description="Updated parent description",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Identity redesign",
                        "user_value": "Tenant access is explicit",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Identity redesign"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Proceed to engineering child fanout.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": [],
                    "ready_to_write": True,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.run_specialist_planning_fanout",
                return_value=SimpleNamespace(
                    planning_state="planning_completed",
                    required_tasks=(),
                    findings=(),
                    recommendations=(),
                    acceptance_impacts=(),
                    open_behavior_questions=(),
                    architecture_summary=(),
                    architecture_diagram="",
                    stages=(),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime",
                side_effect=RuntimeError(
                    'Failed to seed Jira issues: Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}'
                ),
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987D")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        run_flow_mock.assert_not_called()
        with self.session_factory() as session:
            workflow = session.get(WorkflowExecution, "parent_planning:TP-987D")
            self.assertIsNotNone(workflow)
            assert workflow is not None
            operations = session.execute(
                select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
            ).scalars().all()
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "jira_child_fanout",
                )
            ).scalar_one()
            latest_attempt = session.execute(
                select(WorkflowOperationAttempt)
                .where(WorkflowOperationAttempt.operation_id == operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number.desc())
            ).scalars().first()
        self.assertEqual(workflow.status, "failed")
        self.assertIn("CONTENT_LIMIT_EXCEEDED", str(workflow.last_error))
        self.assertEqual(
            {item.operation_type: item.status for item in operations}["backlog_planning"],
            "completed",
        )
        self.assertEqual(operation.status, "failed")
        self.assertIn("CONTENT_LIMIT_EXCEEDED", str(operation.summary))
        self.assertIsNotNone(latest_attempt)
        assert latest_attempt is not None
        self.assertEqual(latest_attempt.status, "failed")
        self.assertEqual(latest_attempt.error_category, "content_limit")
        self.assertTrue(latest_attempt.retryable)

    def test_webhook_pm_parent_issue_updated_posts_formatted_jira_clarification_comment(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-987B", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_updated"
        payload["issue"]["fields"]["reporter"] = {"accountId": "jira-user-987B", "displayName": "Casey Reporter"}
        payload["changelog"] = {"items": [{"field": "description"}]}

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-987B",
                    summary="Identity redesign",
                    status="Backlog",
                    description="Updated parent description",
                    labels=["pm-parent", "sync-blocked"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with self.session_factory() as session:
            persist_parent_feature_brief_snapshot(
                session=session,
                tenant_id="tenant-webhook",
                project_id="project-1",
                parent_issue_key="TP-987B",
                source_text="Existing parent description",
                brief={"objective": "Identity redesign"},
                notes={"source": "test"},
                status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
            )
            upsert_followup_context(
                session=session,
                tenant_id="tenant-webhook",
                project_id="project-1",
                context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
                channel_id="TP-987B",
                issue_key="TP-987B",
                request_id="pm-interview-jira:TP-987B",
                root_message_id="jira-comment-987B",
                metadata={
                    "parent_issue_key": "TP-987B",
                    "questions": ["Old question"],
                    "transport": "jira_issue_comment",
                    "reply_scope": "issue_comment_stream_from_root",
                    "pm_request_id": "pm-request-987B",
                },
            )
            session.commit()

        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Identity redesign",
                        "user_value": "Tenant access is explicit",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Identity redesign"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Clarify MFA fallback and tenant metadata exposure.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": [
                        {
                            "id": "Q-1",
                            "question": "What must happen on a step-up-protected action when MFA is not enabled?",
                            "why_it_matters": "This defines the baseline assurance contract.",
                        },
                        {
                            "id": "Q-2",
                            "question": "What tenant metadata can be shown before entry?",
                            "why_it_matters": "This closes tenant-enumeration leakage.",
                        },
                    ],
                    "ready_to_write": False,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_publishers.resolve_parent_feature_case",
                return_value=SimpleNamespace(
                    request_id="pm-request-987B",
                    channel_id="discord-channel-1",
                    thread_channel_id="discord-thread-1",
                    root_message_id="root-msg-987B",
                    owner_user_id="discord-user-1",
                    source_kind="jira_parent",
                ),
            ),
            patch("orchestrator.core.jira_parent_child_sync_publishers.resolve_platform_secret_ref", return_value="discord-token"),
            patch("orchestrator.core.jira_parent_child_sync_publishers.DiscordApiClient") as discord_client_cls,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment", return_value=({"id": "jira-comment-987B-2"}, None)) as create_comment_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            discord_client = discord_client_cls.return_value
            discord_client.post_message.return_value = {"id": "discord-msg-987B"}
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987B")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()
        run_flow_mock.assert_not_called()
        discord_client.post_message.assert_called_once()
        create_comment_mock.assert_called_once()
        created_comment = create_comment_mock.call_args.kwargs["comment"]
        ordered_questions = created_comment["content"][1]["content"]
        self.assertEqual(len(ordered_questions), 2)
        self.assertIn(
            "What must happen on a step-up-protected action when MFA is not enabled?",
            ordered_questions[0]["content"][0]["content"][0]["text"],
        )
        self.assertIn(
            "Why it matters: This defines the baseline assurance contract.",
            ordered_questions[0]["content"][1]["content"][0]["text"],
        )
        with self.session_factory() as session:
            followups = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-987B",
                    FollowupContext.context_type == "pm_interview",
                )
            ).scalars().all()
        jira_followup = next(row for row in followups if row.channel_id == "TP-987B")
        self.assertEqual(jira_followup.root_message_id, "jira-comment-987B-2")

    def test_webhook_pm_parent_issue_updated_same_open_questions_is_idempotent(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-987B2", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_updated"
        payload["issue"]["fields"]["reporter"] = {"accountId": "jira-user-987B2", "displayName": "Casey Reporter"}
        payload["changelog"] = {"items": [{"field": "description"}]}
        open_questions = [
            {
                "id": "Q-1",
                "question": "What must happen on a step-up-protected action when MFA is not enabled?",
                "why_it_matters": "This defines the baseline assurance contract.",
            },
            {
                "id": "Q-2",
                "question": "What tenant metadata can be shown before entry?",
                "why_it_matters": "This closes tenant-enumeration leakage.",
            },
        ]
        rendered_description = build_parent_feature_description(
            objective="Identity redesign",
            user_value="Tenant access is explicit",
            recommendation="Clarify MFA fallback and tenant metadata exposure.",
            scope_in=["Identity redesign"],
            scope_out=[],
            acceptance_criteria=["Planning can proceed"],
            ui_references=[],
            success_outcomes=[],
            dependencies_and_risks=[],
            open_questions=[item["question"] for item in open_questions],
            parent_revision="normalized-parent-brief",
            sync_status="sync-blocked",
            pm_status="pm_completed",
            planning_state="brief_normalized",
            architecture_summary=None,
            architecture_diagram=None,
        )

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-987B2",
                    summary="Identity redesign",
                    status="Backlog",
                    description=rendered_description,
                    labels=["pm-parent", "sync-blocked"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with self.session_factory() as session:
            persist_parent_feature_brief_snapshot(
                session=session,
                tenant_id="tenant-webhook",
                project_id="project-1",
                parent_issue_key="TP-987B2",
                source_text="Existing parent description",
                brief={"objective": "Identity redesign"},
                notes={"source": "test"},
                status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
            )
            upsert_followup_context(
                session=session,
                tenant_id="tenant-webhook",
                project_id="project-1",
                context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
                channel_id="TP-987B2",
                issue_key="TP-987B2",
                request_id="pm-interview-jira:TP-987B2",
                root_message_id="jira-comment-987B2",
                metadata={
                    "parent_issue_key": "TP-987B2",
                    "questions": open_questions,
                    "transport": "jira_issue_comment",
                    "reply_scope": "issue_comment_stream_from_root",
                    "pm_request_id": "pm-request-987B2",
                },
            )
            session.commit()

        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Identity redesign",
                        "user_value": "Tenant access is explicit",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Identity redesign"],
                        "scope_out": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Clarify MFA fallback and tenant metadata exposure.",
                        "open_questions": [item["question"] for item in open_questions],
                        "next_steps": [],
                    },
                    "open_questions": open_questions,
                    "ready_to_write": False,
                },
            ),
            patch(
                "orchestrator.core.jira_parent_child_sync_publishers.resolve_parent_feature_case",
                return_value=SimpleNamespace(
                    request_id="pm-request-987B2",
                    channel_id="discord-channel-1",
                    thread_channel_id="discord-thread-1",
                    root_message_id="root-msg-987B2",
                    owner_user_id="discord-user-1",
                    source_kind="jira_parent",
                ),
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment", return_value=({"id": "jira-comment-987B2-2"}, None)) as create_comment_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987B2")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        create_comment_mock.assert_not_called()
        run_flow_mock.assert_not_called()

    def test_webhook_pm_parent_issue_updated_with_no_children_posts_no_sync_note(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-987C", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_updated"
        payload["changelog"] = {"items": [{"field": "description"}]}

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-987C",
                    summary="Identity redesign",
                    status="Backlog",
                    description="Updated parent description",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Identity redesign",
                        "user_value": "Tenant access is explicit",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Identity redesign"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Planning can proceed.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": [],
                    "ready_to_write": True,
                },
            ),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.list_child_issue_previews_for_parent", return_value=[]),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as post_comment_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment") as create_comment_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987C")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        run_flow_mock.assert_not_called()
        post_comment_mock.assert_not_called()
        create_comment_mock.assert_not_called()

    def test_webhook_pm_parent_issue_created_blocked_questions_create_new_pm_thread_when_missing(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-988", labels=["pm-parent"], status_name="Backlog")
        payload["webhookEvent"] = "jira:issue_created"

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_fields: list[dict] = []
                self.replaced_labels: list[dict] = []

            def get_issue_detail(self, **kwargs):
                return JiraIssueDetail(
                    key="TP-988",
                    summary="Runtime architecture reset",
                    status="Backlog",
                    description="Loose parent description",
                    labels=["pm-parent", "sync-current"],
                )

            def update_issue_fields(self, **kwargs):
                self.updated_fields.append(kwargs)
                return None

            def replace_issue_labels(self, **kwargs):
                self.replaced_labels.append(kwargs)
                return None

        discord_client = MagicMock()
        discord_client.post_message.side_effect = [
            {"id": "discord-root-988"},
            {"id": "discord-thread-msg-988"},
        ]
        discord_client.create_thread_from_message.return_value = "discord-thread-988"
        oauth_context = SimpleNamespace(
            client=_FakeClient(),
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        )
        with self.session_factory() as session:
            project = session.get(Project, "tenant-webhook-default")
            assert project is not None
            project.discord_config = {"channel_id": "discord-channel-1"}
            session.commit()
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=object()),
            patch(
                "orchestrator.core.jira_parent_child_sync_service.normalize_parent_feature_brief_with_runtime",
                return_value={
                    "brief": {
                        "objective": "Runtime architecture reset",
                        "user_value": "Orchestration behavior is easier to evolve",
                        "acceptance_criteria": ["Planning can proceed"],
                        "scope_in": ["Architecture reset"],
                        "scope_out": [],
                        "ui_references": [],
                        "constraints": [],
                        "risks": [],
                        "success_outcomes": [],
                        "recommendation": "Clarify the release-train ownership model before planning.",
                        "open_questions": [],
                        "next_steps": [],
                    },
                    "open_questions": ["Should release-train supervision stay on the parent PM flow or move to a child?"],
                    "ready_to_write": False,
                },
            ),
            patch("orchestrator.core.jira_parent_child_sync_publishers.resolve_platform_secret_ref", return_value="discord-bot-token"),
            patch("orchestrator.core.jira_parent_child_sync_publishers.DiscordApiClient", return_value=discord_client),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.create_jira_comment", return_value=({"id": "jira-comment-988"}, None)),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-988")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()
        run_flow_mock.assert_not_called()
        discord_client.create_thread_from_message.assert_called_once()
        with self.session_factory() as session:
            followups = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-988",
                    FollowupContext.context_type == "pm_interview",
                )
            ).scalars().all()
            interview_case = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "pm-parent-interview:TP-988")
            ).scalars().one_or_none()
            project = session.get(Project, "tenant-webhook-default")
        jira_followup = next(row for row in followups if row.channel_id == "TP-988")
        discord_followup = next(row for row in followups if row.thread_channel_id == "discord-thread-988")
        self.assertIsNotNone(interview_case)
        self.assertIsNotNone(project)
        assert interview_case is not None
        assert project is not None
        self.assertEqual(jira_followup.root_message_id, "jira-comment-988")
        self.assertEqual(discord_followup.thread_channel_id, "discord-thread-988")
        self.assertEqual(interview_case.thread_channel_id, "discord-thread-988")
        self.assertEqual(interview_case.status, "question_pending")
        self.assertIn("discord-thread-988", (project.discord_config or {}).get("ask_thread_channel_ids", []))
