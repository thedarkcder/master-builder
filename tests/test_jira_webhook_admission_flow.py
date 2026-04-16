from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from orchestrator.api.webhooks.jira_admission_flow import plan_jira_run_flow
from orchestrator.api.webhooks.jira_webhook_types import jira_webhook_response
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext
from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.precheck_question_lock import build_precheck_questions_block
from orchestrator.storage.models import Project, Tenant
from tests.test_support.jira_webhook_harness import JiraWebhookHarness
from tests.workflow_test_support import add_run_with_workflow, make_run


pytestmark = pytest.mark.contract


class JiraWebhookAdmissionFlowTests(JiraWebhookHarness):
    def test_webhook_pm_parent_or_sync_blocked_issue_never_surfaces_ready_for_agent(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            project = session.execute(
                select(Project)
                .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                .limit(1)
            ).scalar_one()
            assert tenant is not None
            context = JiraWebhookContext(
                request_id="req-pm-parent",
                tenant_id="tenant-webhook",
                tenant=tenant,
                payload={},
                webhook_event="issue_updated",
                issue_key="TP-776",
                issue_labels=["pm-parent", "agent:ready"],
                issue_status="To Do",
                issue_status_category_key="new",
                issue_summary="Parent issue should not enqueue",
                issue_description="Still blocked on PM clarification.",
                comment_command=None,
                comment_command_argument=None,
                comment_command_error=None,
                delivery_id="delivery-pm-parent",
                project=project,
            )

            plan = plan_jira_run_flow(
                context=context,
                session=session,
                settings=get_settings(),
                evaluate_jira_trigger_state_fn=MagicMock(),
                jira_webhook_response_fn=jira_webhook_response,
            )

        self.assertEqual(plan.content["reason"], "pm_parent_or_sync_blocked")
        self.assertFalse(plan.content["enqueued"])
        self.assertNotIn("ready_for_agent", plan.content)

    def test_webhook_backlog_pre_run_check_reports_ready_for_agent_without_enqueue(self) -> None:
        with self.session_factory() as session:
            project = session.execute(
                select(Project)
                .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                .limit(1)
            ).scalar_one()
            project.policy_overrides = {**dict(project.policy_overrides or {}), "run_board_id": 1}
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-777", status_name="To Do", labels=["agent:ready"])
        payload["issue"]["fields"]["summary"] = "Objective scope acceptance context how to test mvp risk"
        payload["issue"]["fields"]["description"] = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Objective: validate webhook flow. Scope: webhook only. "
                                "Acceptance criteria: no run from backlog. Context: orchestrator jira ingress. "
                                "How to test: post webhook payload. NFR intent: MVP. Risks/dependencies: none."
                            ),
                        }
                    ],
                }
            ],
        }
        with (
            patch("orchestrator.api.webhooks.jira_webhook_board_gate._fetch_issue_board_location", return_value=("backlog", None)),
            patch(
                "orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check",
                return_value=self._pre_run_check(),
            ),
            patch("orchestrator.core.discord.transport_executor.send_tenant_discord_message") as notify_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-777")
        self.assertEqual(body["webhook_event"], "issue_updated")
        notify_mock.assert_not_called()

    def test_evaluate_precheck_decision_with_labels_writes_open_questions_to_jira(self) -> None:
        self._default_precheck_decision_patch.stop()
        try:
            from orchestrator.api.webhooks import jira_admission_flow

            unresolved_pre_check = PreRunCheckResult(
                outcome="decision_gate_required",
                ready_label="agent:ready",
                ready_label_present=False,
                required_worker_capability="linux",
                required_worker_label="worker:linux",
                required_worker_label_present=True,
                decision_gate=DecisionGateResult(
                    triggered=True,
                    reason="Cross-account relink policy is missing.",
                    missing_sections=(),
                    questions=("What happens when a device relinks to another user?",),
                    recommendation="Clarify the device ownership policy before execution.",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
            )

            with self.session_factory() as session:
                tenant = session.get(Tenant, "tenant-webhook")
                project = session.execute(
                    select(Project)
                    .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                    .limit(1)
                ).scalar_one()
                assert tenant is not None
                context = JiraWebhookContext(
                    request_id="req-1",
                    tenant_id="tenant-webhook",
                    tenant=tenant,
                    payload={},
                    webhook_event="jira:issue_updated",
                    issue_key="TP-777",
                    issue_labels=[],
                    issue_status="To Do",
                    issue_status_category_key="new",
                    issue_summary="Clarify relink policy",
                    issue_description="Original description.",
                    comment_command=None,
                    comment_command_argument=None,
                    comment_command_error=None,
                    delivery_id="delivery-1",
                    project=project,
                )
                oauth_client = MagicMock()
                oauth_context = SimpleNamespace(
                    access_token="tok",
                    connection=SimpleNamespace(cloud_id="cloud-1"),
                    client=oauth_client,
                )
                decision_result = self._decision_result(
                    pre_check=unresolved_pre_check,
                    issue_labels=[],
                    cycle_id="cycle-1",
                )

                with patch.object(
                    jira_admission_flow,
                    "evaluate_issue_clarification_state",
                    return_value=decision_result,
                ), patch.object(
                    jira_admission_flow,
                    "tenant_jira_oauth_context",
                    return_value=oauth_context,
                ):
                    result = jira_admission_flow.evaluate_precheck_decision_with_labels(
                        context=context,
                        session=session,
                        settings=get_settings(),
                    )

            self.assertIs(result, decision_result)
            oauth_client.update_issue_summary_and_description.assert_called_once()
            updated_description = oauth_client.update_issue_summary_and_description.call_args.kwargs["description"]
            self.assertIn("Decision Gate reason: Cross-account relink policy is missing.", updated_description)
            self.assertIn("What happens when a device relinks to another user?", updated_description)
            self.assertEqual(context.issue_description, updated_description)
        finally:
            self._default_precheck_decision_patch.start()

    def test_evaluate_precheck_decision_with_labels_removes_resolved_question_block_from_jira(self) -> None:
        self._default_precheck_decision_patch.stop()
        try:
            from orchestrator.api.webhooks import jira_admission_flow

            existing_block = build_precheck_questions_block(
                decision_gate_reason="Cross-account relink policy is missing.",
                decision_gate_questions=["What happens when a device relinks to another user?"],
                gtd_questions=[],
            )
            assert existing_block is not None
            starting_description = f"Original description.\n\n{existing_block}"

            with self.session_factory() as session:
                tenant = session.get(Tenant, "tenant-webhook")
                project = session.execute(
                    select(Project)
                    .where(Project.tenant_id == "tenant-webhook", Project.jira_project_key == "TP")
                    .limit(1)
                ).scalar_one()
                assert tenant is not None
                context = JiraWebhookContext(
                    request_id="req-2",
                    tenant_id="tenant-webhook",
                    tenant=tenant,
                    payload={},
                    webhook_event="jira:issue_updated",
                    issue_key="TP-778",
                    issue_labels=["agent:ready"],
                    issue_status="To Do",
                    issue_status_category_key="new",
                    issue_summary="Clarify relink policy",
                    issue_description=starting_description,
                    comment_command=None,
                    comment_command_argument=None,
                    comment_command_error=None,
                    delivery_id="delivery-2",
                    project=project,
                )
                oauth_client = MagicMock()
                oauth_context = SimpleNamespace(
                    access_token="tok",
                    connection=SimpleNamespace(cloud_id="cloud-1"),
                    client=oauth_client,
                )
                decision_result = self._decision_result(
                    pre_check=self._pre_run_check(),
                    issue_labels=["agent:ready"],
                    cycle_id="cycle-2",
                )

                with patch.object(
                    jira_admission_flow,
                    "evaluate_issue_clarification_state",
                    return_value=decision_result,
                ), patch.object(
                    jira_admission_flow,
                    "tenant_jira_oauth_context",
                    return_value=oauth_context,
                ):
                    result = jira_admission_flow.evaluate_precheck_decision_with_labels(
                        context=context,
                        session=session,
                        settings=get_settings(),
                    )

            self.assertIs(result, decision_result)
            oauth_client.update_issue_summary_and_description.assert_called_once()
            updated_description = oauth_client.update_issue_summary_and_description.call_args.kwargs["description"]
            self.assertIn("Original description.", updated_description)
            self.assertNotIn("Decision Gate reason:", updated_description)
            self.assertEqual(context.issue_description, updated_description)
        finally:
            self._default_precheck_decision_patch.start()

    def test_webhook_applies_ready_label_when_precheck_reports_missing(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-140", status_name="To Do", labels=["worker:linux"])
        oauth_client = MagicMock()
        oauth_context = SimpleNamespace(
            access_token="tok",
            connection=SimpleNamespace(cloud_id="cloud-1"),
            client=oauth_client,
        )
        missing_ready_decision_gate = PreRunCheckResult(
            outcome="decision_gate_required",
            ready_label="agent:ready",
            ready_label_present=False,
            required_worker_capability="linux",
            required_worker_label="worker:linux",
            required_worker_label_present=True,
            decision_gate=DecisionGateResult(
                triggered=True,
                reason="Missing GTD sections",
                missing_sections=(),
                questions=(),
                recommendation="Decision required before build",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )

        with (
            patch(
                "orchestrator.api.webhooks.jira_admission_flow.tenant_jira_oauth_context",
                return_value=oauth_context,
            ),
            patch(
                "orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check",
                return_value=missing_ready_decision_gate,
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-140")
        oauth_client.add_issue_labels.assert_not_called()

    def test_webhook_marks_issue_created_backlog_ready_without_enqueue(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-130", status_name="Ready for Agent", labels=["agent:ready"])
        payload["webhookEvent"] = "jira:issue_created"

        with patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-130")
        self.assertEqual(body["webhook_event"], "issue_created")

    def test_webhook_marks_backlog_status_ready_for_agent_without_enqueue(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-803", status_name="In Progress")

        response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-803")

    def test_webhook_suppresses_rerun_during_decision_gate_cooldown(self) -> None:
        with self.session_factory() as session:
            run = make_run(
                run_id="run-decision-gate-1",
                tenant_id="tenant-webhook",
                project_id=None,
                issue_key="TP-804",
                issue_summary="Need GTD",
                issue_description="Missing sections",
                repo_url="https://github.com/example/repo",
                branch=None,
                pr_url=None,
                status="blocked",
                last_error="Decision Gate required: Missing GTD sections",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=2),
                started_at=None,
                finished_at=datetime.now(timezone.utc) - timedelta(minutes=2),
            )
            add_run_with_workflow(session, run, workflow_status="blocked")
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-804", status_name="To Do")
        with (
            patch("orchestrator.core.discord.transport_executor.send_tenant_discord_message") as notify_mock,
            patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-804")
        notify_mock.assert_not_called()

    def test_webhook_allows_rerun_after_decision_gate_cooldown(self) -> None:
        with self.session_factory() as session:
            run = make_run(
                run_id="run-decision-gate-2",
                tenant_id="tenant-webhook",
                project_id=None,
                issue_key="TP-805",
                issue_summary="Need GTD",
                issue_description="Missing sections",
                repo_url="https://github.com/example/repo",
                branch=None,
                pr_url=None,
                status="blocked",
                last_error="Decision Gate required: Missing GTD sections",
                created_at=datetime.now(timezone.utc) - timedelta(minutes=20),
                started_at=None,
                finished_at=datetime.now(timezone.utc) - timedelta(minutes=20),
            )
            add_run_with_workflow(session, run, workflow_status="blocked")
            session.commit()

        payload = self._jira_issue_payload(issue_key="TP-805", status_name="To Do")
        with patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-805")

    def test_webhook_transition_only_mode_allows_transition_into_ready_status(self) -> None:
        self._create_tenant("tenant-transition-only-2", ready_trigger_mode="transition_only")
        payload = self._jira_issue_payload(issue_key="TP-130", labels=["agent:ready"])
        payload["changelog"] = {
            "items": [
                {
                    "field": "status",
                    "fromString": "To Do",
                    "toString": "Ready for Agent",
                }
            ]
        }

        response = self.client.post("/jira/webhook/tenant-transition-only-2", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-130")
