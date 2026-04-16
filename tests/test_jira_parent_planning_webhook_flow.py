from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from orchestrator.api.webhooks.jira_application import JiraWebhookPlan
from orchestrator.core.pm_interview_service import PM_INTERVIEW_STATUS_QUESTION_PENDING
from orchestrator.core.parent_feature_brief_store import persist_parent_feature_brief_snapshot
from orchestrator.storage.models import FollowupContext, PMInterviewCase, Project, Tenant
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
                "orchestrator.core.jira_parent_child_sync_service.resolve_parent_feature_case",
                return_value=SimpleNamespace(
                    request_id="pm-request-987",
                    channel_id="discord-channel-1",
                    thread_channel_id="discord-thread-1",
                    root_message_id="root-msg-1",
                    owner_user_id="discord-user-1",
                ),
            ),
            patch("orchestrator.core.jira_parent_child_sync_service.resolve_platform_secret_ref", return_value="discord-bot-token"),
            patch("orchestrator.core.jira_parent_child_sync_service.DiscordApiClient", return_value=discord_client),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
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
        self.assertGreaterEqual(comment_mock.call_count, 1)
        jira_question_comment = comment_mock.call_args_list[0].kwargs["comment"]
        self.assertEqual(jira_question_comment["type"], "doc")
        intro_content = jira_question_comment["content"][0]["content"]
        self.assertEqual(intro_content[0]["type"], "mention")
        self.assertEqual(intro_content[0]["attrs"]["id"], "jira-user-987")
        ordered_questions = jira_question_comment["content"][1]["content"]
        self.assertEqual(len(ordered_questions), 1)
        self.assertIn("Who owns release-train supervision", ordered_questions[0]["content"][0]["content"][0]["text"])
        with self.session_factory() as session:
            followup = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-987",
                    FollowupContext.context_type == "pm_interview",
                )
            ).scalars().one_or_none()
        self.assertIsNotNone(followup)
        assert followup is not None
        self.assertEqual(followup.thread_channel_id, "discord-thread-1")
        self.assertEqual(followup.request_id, "pm-request-987")
        self.assertEqual(followup.status, "active")
        with self.session_factory() as session:
            snapshot = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "parent-brief:TP-987")
            ).scalars().one()
        self.assertEqual(snapshot.status, PM_INTERVIEW_STATUS_QUESTION_PENDING)

    def test_webhook_pm_parent_issue_created_blocks_cleanly_when_discord_followup_fails(self) -> None:
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
                "orchestrator.core.jira_parent_child_sync_service.resolve_parent_feature_case",
                return_value=SimpleNamespace(
                    request_id="pm-request-987A",
                    channel_id="discord-channel-1",
                    thread_channel_id="discord-thread-1",
                    root_message_id="root-msg-1",
                    owner_user_id="discord-user-1",
                ),
            ),
            patch("orchestrator.core.jira_parent_child_sync_service.resolve_platform_secret_ref", return_value=None),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_runtime") as seed_mock,
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow") as run_flow_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-987A")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()
        run_flow_mock.assert_not_called()
        self.assertTrue(oauth_context.client.updated_fields)
        self.assertEqual(len(oauth_context.client.replaced_labels), 1)
        self.assertGreaterEqual(comment_mock.call_count, 1)
        blocked_comment = comment_mock.call_args_list[0].kwargs["comment"]
        self.assertEqual(blocked_comment["type"], "doc")
        self.assertEqual(blocked_comment["content"][0]["content"][0]["attrs"]["id"], "jira-user-987A")
        self.assertIn("Discord PM follow-up could not be created", blocked_comment["content"][2]["content"][0]["text"])
        with self.session_factory() as session:
            followup = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-987A",
                    FollowupContext.context_type == "pm_interview",
                )
            ).scalars().one_or_none()
            snapshot = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "parent-brief:TP-987A")
            ).scalars().one()
        self.assertIsNone(followup)
        self.assertEqual(snapshot.status, PM_INTERVIEW_STATUS_QUESTION_PENDING)

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
            patch("orchestrator.core.jira_parent_child_sync_service.resolve_platform_secret_ref", return_value="discord-bot-token"),
            patch("orchestrator.core.jira_parent_child_sync_service.DiscordApiClient", return_value=discord_client),
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
            followup = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "tenant-webhook",
                    FollowupContext.issue_key == "TP-988",
                    FollowupContext.context_type == "pm_interview",
                )
            ).scalars().one_or_none()
            interview_case = session.execute(
                select(PMInterviewCase).where(PMInterviewCase.request_id == "pm-parent-interview:TP-988")
            ).scalars().one_or_none()
            project = session.get(Project, "tenant-webhook-default")
        self.assertIsNotNone(followup)
        self.assertIsNotNone(interview_case)
        self.assertIsNotNone(project)
        assert followup is not None
        assert interview_case is not None
        assert project is not None
        self.assertEqual(followup.thread_channel_id, "discord-thread-988")
        self.assertEqual(interview_case.thread_channel_id, "discord-thread-988")
        self.assertEqual(interview_case.status, "question_pending")
        self.assertIn("discord-thread-988", (project.discord_config or {}).get("ask_thread_channel_ids", []))
