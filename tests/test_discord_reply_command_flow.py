from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.decision.planner import DecisionPlannerQuestion, DecisionPlannerResult
from orchestrator.core.decision.gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck.pre_run_check import PreRunCheckResult
from orchestrator.core.runtime.payload_models import InteractionAction, InteractionResponse
from orchestrator.tools.atlassian_oauth import JiraIssueDetail, JiraIssuePreview
from tests.test_support.discord_command_reply_harness import DiscordCommandReplyHarness


pytestmark = pytest.mark.contract


class DiscordReplyCommandFlowTests(DiscordCommandReplyHarness):
    def test_reply_updates_jira_from_dict_oauth_context_and_enqueues_retry(self) -> None:
        self._queue_run(run_id="run-failed-reply-1", issue_key="TP-88", status="failed")
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(summary="Old summary", description="Objective: old")
            ),
            update_issue_summary_and_description=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        runtime = __import__("unittest").mock.MagicMock()
        runtime.run_json.return_value = {
            "summary": "Updated summary",
            "objective": "Clear onboarding objective.",
            "scope": "Splash to onboarding to demo flow.",
            "acceptance_criteria": "Flow and guards verified.",
            "how_to_test": "Run listed scenario checks.",
            "nfr_intent": "MVP-first, scale-aware.",
            "reliability_security_constraints": "Fail-closed on unknown state.",
            "out_of_scope": "Real StoreKit and Supabase integration.",
            "rollout_constraints": "No migration required.",
            "dependencies_and_risks": [
                "Supabase evaluate-session must be deployed",
                "Function latency may delay second-attempt eligibility",
            ],
            "decision_owner": "Product Owner / Founder.",
        }

        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-88", summary="Retry from reply", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-88",
                    summary="Retry from reply",
                    status="To Do",
                    description="Objective: refreshed for retry.",
                ),
            ),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.discord.ingress.executor.build_codex_runtime", return_value=runtime),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck",
                return_value=SimpleNamespace(
                    capture=SimpleNamespace(
                        cycle=SimpleNamespace(cycle_id="cycle-1"),
                        effect_ids=(),
                        evidence_id="evidence-1",
                    ),
                    decision_result=SimpleNamespace(
                        decision=SimpleNamespace(
                            pre_check=PreRunCheckResult(
                                outcome="ready_for_agent",
                                ready_label="agent:ready",
                                ready_label_present=True,
                                required_worker_capability="linux",
                                required_worker_label="worker:linux",
                                required_worker_label_present=True,
                                decision_gate=DecisionGateResult(
                                    triggered=False,
                                    reason="Decision Gate not required",
                                    missing_sections=(),
                                    questions=(),
                                    recommendation="Proceed",
                                    tags=(),
                                ),
                                gtd=GoodToDoValidationResult(
                                    valid=True,
                                    missing_criteria=(),
                                    clarification_questions=(),
                                ),
                            ),
                        ),
                        classification="clear",
                        missing_slots=[],
                        auto_resolved_slots=[],
                        cycle_id=None,
                    ),
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._ready_decision_result(),
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-88 Objective and testing details",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "retry")
        oauth_client.get_issue_detail.assert_called_once()
        oauth_client.update_issue_summary_and_description.assert_not_called()

    def test_reply_with_incomplete_oauth_context_returns_controlled_502(self) -> None:
        self._queue_run(run_id="run-failed-reply-2", issue_key="TP-89", status="failed")

        with patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value={"access_token": "tok-only"}):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-89 objective details",
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to update Jira context", response.json()["detail"])
        self.assertIn("`TP-89`", response.json()["detail"])
        self.assertNotIn("tok-only", response.json()["detail"])
        self.assertNotIn("Internal server error. Ref:", response.json()["detail"])

    def test_reply_when_decision_gate_still_triggered_returns_recheck_not_retry(self) -> None:
        self._queue_run(run_id="run-failed-reply-3", issue_key="TP-90", status="failed")
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(summary="Old summary", description="Objective: old")
            ),
            update_issue_summary_and_description=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        planner_result = DecisionPlannerResult(
            gate_status="blocked_decision_gate",
            reason="Missing GTD sections",
            questions=(
                DecisionPlannerQuestion(
                    question_id="dg_1",
                    kind="decision_gate",
                    question="What entitlement/capability values are required for production and staging?",
                    status="open",
                    detail="Config values were captured, but entitlement confirmation is still missing.",
                ),
            ),
            question_states=(
                DecisionPlannerQuestion(
                    question_id="dg_1",
                    kind="decision_gate",
                    question="Objective?",
                    status="answered",
                    detail="Config values were captured, but entitlement confirmation is still missing.",
                ),
            ),
            resolved_items=(),
            missing_items=(),
            captured_answer_summary=None,
        )
        with (
            patch("orchestrator.api.discord.commands.run_controls.dispatch_run_control_command") as dispatch_mock,
            patch("orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview") as preview_mock,
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck",
                return_value=SimpleNamespace(
                    capture=SimpleNamespace(evidence_id="evidence-2"),
                    decision_result=SimpleNamespace(
                        decision=SimpleNamespace(
                            pre_check=PreRunCheckResult(
                                outcome="decision_gate_required",
                                ready_label="agent:ready",
                                ready_label_present=True,
                                required_worker_capability="linux",
                                required_worker_label="worker:linux",
                                required_worker_label_present=True,
                                decision_gate=DecisionGateResult(
                                    triggered=True,
                                    reason="Missing GTD sections",
                                    missing_sections=(),
                                    questions=("Objective?", "How to test?"),
                                    recommendation="Clarification required",
                                    tags=(),
                                ),
                                gtd=GoodToDoValidationResult(
                                    valid=True,
                                    missing_criteria=(),
                                    clarification_questions=(),
                                ),
                            ),
                        ),
                        classification="decision_gate",
                        missing_slots=[],
                        auto_resolved_slots=[],
                        cycle_id="cycle-1",
                    ),
                ),
            ),
            patch("orchestrator.core.decision.engine.plan_decision_questions", return_value=planner_result),
            patch(
                "orchestrator.api.discord.commands.run_controls.load_cycle_question_feedback",
                return_value=(
                    {
                        "question_id": "dg_1",
                        "question_text": "What entitlement/capability values are required for production and staging?",
                        "note": "Config values were captured, but entitlement confirmation is still missing.",
                        "status": "open",
                    },
                ),
            ),
            patch("orchestrator.api.discord.commands.run_controls.build_runtime_precheck_message") as build_message_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-90 objective details",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "reply")
        self.assertTrue(response.json()["data"]["recheck_required"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-90")
        self.assertEqual(
            response.json()["data"]["questions"],
            ["What entitlement/capability values are required for production and staging?"],
        )
        self.assertEqual(
            response.json()["data"]["question_feedback"],
            [
                {
                    "note": "Config values were captured, but entitlement confirmation is still missing.",
                    "question_id": "dg_1",
                    "question_text": "What entitlement/capability values are required for production and staging?",
                    "status": "open",
                }
            ],
        )
        self.assertIn("Config values were captured, but entitlement confirmation is still missing.", response.json()["message"])
        oauth_client.update_issue_summary_and_description.assert_not_called()
        dispatch_mock.assert_not_called()
        preview_mock.assert_not_called()
        build_message_mock.assert_not_called()

    def test_reply_question_returns_conversation_response_without_recheck(self) -> None:
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(summary="HubSpot billing", description="Objective: clarify HubSpot subscription rules")
            ),
            add_issue_comment=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        question_feedback = (
            {
                "question_id": "decision_owner",
                "question_text": "Who is the single accountable decision owner for TP-248?",
                "answer": "stake holder",
                "note": "Current answer 'stake holder' is not a specific accountable person.",
                "status": "answered",
            },
            {
                "question_id": "hubspot_subscription_rules",
                "question_text": "Which HubSpot information should decide whether the customer subscription is active, overdue, cancelled, or expired?",
                "note": "Annual invoice is confirmed as billing source, but the business rules for subscription status are not clear yet.",
                "status": "answered",
            },
        )

        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.discord.ingress.executor.build_codex_runtime", return_value=SimpleNamespace()),
            patch(
                "orchestrator.api.discord.commands.run_controls.active_case_and_cycle_for_issue",
                return_value=(SimpleNamespace(case_id="case-1"), SimpleNamespace(cycle_id="cycle-1")),
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.list_cycle_answers",
                return_value=[],
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.interpret_decision_reply",
                return_value=InteractionResponse(
                    message=(
                        "I need to know which HubSpot information the product should trust before we link the "
                        "tenant subscription, and which business statuses should make access active, overdue, "
                        "cancelled, or expired."
                    ),
                    actions=(),
                ),
            ) as interpret_mock,
            patch(
                "orchestrator.api.discord.commands.run_controls.load_cycle_question_feedback",
                return_value=question_feedback,
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck"
            ) as reply_recheck_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-248 What fields do you need?",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["command"], "reply")
        self.assertTrue(body["data"]["conversation_response"])
        self.assertIn("which HubSpot information the product should trust", body["message"])
        self.assertIn("which business statuses should make access", body["message"])
        self.assertNotIn("object-to-field", body["message"])
        self.assertNotIn("properties", body["message"])
        self.assertNotIn("plain English", body["message"])
        self.assertNotIn("Please reply with:", body["message"])
        self.assertEqual(interpret_mock.call_args.kwargs["issue_summary"], "HubSpot billing")
        self.assertEqual(interpret_mock.call_args.kwargs["issue_description"], "Objective: clarify HubSpot subscription rules")
        reply_recheck_mock.assert_not_called()
        oauth_client.add_issue_comment.assert_not_called()

    def test_reply_capture_action_without_recheck_persists_without_rechecking(self) -> None:
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(summary="HubSpot billing", description="Objective: clarify HubSpot subscription rules")
            ),
            add_issue_comment=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }

        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.discord.ingress.executor.build_codex_runtime", return_value=SimpleNamespace()),
            patch(
                "orchestrator.api.discord.commands.run_controls.active_case_and_cycle_for_issue",
                return_value=(SimpleNamespace(case_id="case-1"), SimpleNamespace(cycle_id="cycle-1")),
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.list_cycle_answers",
                return_value=[],
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.interpret_decision_reply",
                return_value=InteractionResponse(
                    message="I have recorded that answer. I still need to know how the business identifies the billing period before this can run.",
                    actions=(
                        InteractionAction(
                            type="capture_decision_answer",
                            payload={
                                "question_id": "hubspot_subscription_rules",
                                "status": "answered",
                                "answer": "Annual invoice is the billing source.",
                                "notes": "Billing source answered; billing-period business meaning is still missing.",
                            },
                        ),
                    ),
                ),
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.capture_decision_reply",
                return_value=SimpleNamespace(evidence_id="evidence-1", effect_ids=("effect-1",)),
            ) as capture_mock,
            patch("orchestrator.api.discord.commands.run_controls.publish_decision_effects") as publish_mock,
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck"
            ) as reply_recheck_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-248 Annual invoice is the billing source",
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["command"], "reply")
        self.assertEqual(
            body["message"],
            "I have recorded that answer. I still need to know how the business identifies the billing period before this can run.",
        )
        self.assertEqual(body["data"]["actions_applied"], ["capture_decision_answer"])
        self.assertFalse(body["data"]["recheck_requested"])
        self.assertEqual(body["data"]["evidence_id"], "evidence-1")
        capture_mock.assert_called_once()
        publish_mock.assert_called_once()
        reply_recheck_mock.assert_not_called()

    def test_reply_uses_captured_evidence_id_for_decision_event_idempotency(self) -> None:
        self._queue_run(run_id="run-failed-reply-idempotency", issue_key="TP-90", status="failed")
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(summary="Old summary", description="Objective: old", labels=["agent:ready"])
            ),
            add_issue_comment=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck"
            ) as reply_recheck_mock,
            patch(
                "orchestrator.api.discord.commands.run_controls.load_cycle_question_feedback",
                return_value=(
                    {
                        "question_id": "dg_1",
                        "question_text": "What entitlement/capability values are required for production and staging?",
                        "note": "Config values were captured, but entitlement confirmation is still missing.",
                        "status": "open",
                    },
                ),
            ),
        ):
            reply_recheck_mock.return_value = SimpleNamespace(
                capture=SimpleNamespace(evidence_id="evidence-123"),
                decision_result=SimpleNamespace(
                    decision=SimpleNamespace(
                        pre_check=PreRunCheckResult(
                            outcome="decision_gate_required",
                            ready_label="agent:ready",
                            ready_label_present=True,
                            required_worker_capability="linux",
                            required_worker_label="worker:linux",
                            required_worker_label_present=True,
                            decision_gate=DecisionGateResult(
                                triggered=True,
                                reason="Need config",
                                missing_sections=(),
                                questions=("Original question",),
                                recommendation="Clarification required",
                                tags=(),
                            ),
                            gtd=GoodToDoValidationResult(
                                valid=True,
                                missing_criteria=(),
                                clarification_questions=(),
                            ),
                        ),
                        block_reason="decision_gate_required",
                    ),
                    classification="decision_gate",
                    missing_slots=[],
                    auto_resolved_slots=[],
                    cycle_id="cycle-1",
                ),
            )
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-90 objective details",
                },
            )

        self.assertEqual(response.status_code, 200)
        decision_event = reply_recheck_mock.call_args.kwargs["decision_event_factory"](
            SimpleNamespace(evidence_id="evidence-123")
        )
        self.assertEqual(decision_event.idempotency_key, "decision-reply:evidence-123")

    def test_reply_when_only_gtd_is_blocking_does_not_surface_decision_gate_reason(self) -> None:
        self._queue_run(run_id="run-failed-reply-gtd", issue_key="TP-90", status="failed")
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(summary="Old summary", description="Objective: old", labels=["agent:ready"])
            ),
            update_issue_summary_and_description=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        planner_result = DecisionPlannerResult(
            gate_status="blocked_gtd",
            reason="Missing GTD criteria",
            questions=(
                DecisionPlannerQuestion(
                    question_id="gtd_1",
                    kind="gtd",
                    question="Which dependencies or risks may impact delivery?",
                    status="open",
                    detail="Dependencies and risks identified",
                ),
            ),
            question_states=(
                DecisionPlannerQuestion(
                    question_id="gtd_1",
                    kind="gtd",
                    question="Which dependencies or risks may impact delivery?",
                    status="open",
                    detail="Dependencies and risks identified",
                ),
            ),
            resolved_items=(),
            missing_items=("Dependencies and risks identified",),
            captured_answer_summary=None,
        )
        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck",
                return_value=SimpleNamespace(
                    capture=SimpleNamespace(evidence_id="evidence-3"),
                    decision_result=SimpleNamespace(
                        decision=SimpleNamespace(
                            pre_check=PreRunCheckResult(
                                outcome="gtd_required",
                                ready_label="agent:ready",
                                ready_label_present=True,
                                required_worker_capability="linux",
                                required_worker_label="worker:linux",
                                required_worker_label_present=True,
                                decision_gate=DecisionGateResult(
                                    triggered=False,
                                    reason="Decision Gate not required",
                                    missing_sections=(),
                                    questions=(),
                                    recommendation="Proceed",
                                    tags=(),
                                ),
                                gtd=GoodToDoValidationResult(
                                    valid=False,
                                    missing_criteria=("Dependencies and risks identified",),
                                    clarification_questions=("Which dependencies or risks may impact delivery?",),
                                ),
                            ),
                        ),
                        classification="gtd",
                        missing_slots=[],
                        auto_resolved_slots=[],
                        cycle_id="cycle-1",
                    ),
                ),
            ),
            patch("orchestrator.core.decision.engine.plan_decision_questions", return_value=planner_result),
            patch(
                "orchestrator.api.discord.commands.run_controls.build_runtime_precheck_message",
                return_value=(
                    "Dependencies and risks identified. Which dependencies or risks may impact delivery?",
                    ["Which dependencies or risks may impact delivery?"],
                ),
            ) as build_message_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-90 objective details",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "reply")
        self.assertEqual(response.json()["data"]["classification"], "gtd")
        self.assertIsNone(response.json()["data"]["decision_gate_reason"])
        self.assertIn("Dependencies and risks identified", response.json()["data"]["gtd_missing_criteria"])
        self.assertIn("Which dependencies or risks may impact delivery?", response.json()["data"]["questions"])
        self.assertNotIn("Decision Gate reason:", response.json()["message"])
        oauth_client.update_issue_summary_and_description.assert_not_called()
        build_message_mock.assert_called_once()

    def test_reply_without_retryable_run_queues_initial_run_after_clarification(self) -> None:
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(summary="Old summary", description="Objective: old", labels=["worker:linux"])
            ),
            update_issue_summary_and_description=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        ready_result = PreRunCheckResult(
            outcome="ready_for_agent",
            ready_label="agent:ready",
            ready_label_present=True,
            required_worker_capability="linux",
            required_worker_label="worker:linux",
            required_worker_label_present=True,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )
        queued_run = SimpleNamespace(run_id="run-new-1", issue_key="TP-91")
        enqueue_result = SimpleNamespace(enqueued=True, run=queued_run, reason=None)

        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck",
                return_value=SimpleNamespace(
                    capture=SimpleNamespace(evidence_id="evidence-4"),
                    decision_result=SimpleNamespace(
                        decision=SimpleNamespace(pre_check=ready_result),
                        issue_labels=["worker:linux", "agent:ready"],
                        classification="clear",
                        missing_slots=[],
                        auto_resolved_slots=[],
                        cycle_id=None,
                    ),
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-91", summary="Run after reply", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-91",
                    summary="Run after reply",
                    status="To Do",
                    description="Objective: refreshed for run.",
                ),
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.enqueue_issue_run_with_precheck",
                return_value=enqueue_result,
            ) as enqueue_mock,
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._ready_decision_result(),
            ) as decision_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-91 objective details",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "run")
        self.assertEqual(response.json()["data"]["issue_key"], "TP-91")
        self.assertEqual(response.json()["data"]["run_id"], "run-new-1")
        oauth_client.update_issue_summary_and_description.assert_not_called()
        enqueue_mock.assert_called_once()
        self.assertEqual(
            decision_mock.call_args.kwargs["event"].issue_labels,
            ["worker:linux", "agent:ready"],
        )

    def test_reply_without_active_decision_cycle_returns_explicit_conflict(self) -> None:
        oauth_client = SimpleNamespace(
            get_issue_detail=MagicMock(
                return_value=SimpleNamespace(
                    summary="Old summary",
                    description=(
                        "Objective: old\n"
                        "<!-- decision-gate-clarifications:start -->\n"
                        "## Decision Gate Clarifications\n"
                        "Objective: stale objective\n"
                        "<!-- decision-gate-clarifications:end -->\n"
                    ),
                )
            ),
            update_issue_summary_and_description=MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }

        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.capture_decision_reply_and_recheck",
                side_effect=ValueError("No active decision cycle exists for TP-92"),
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!reply TP-92 Replace stale block",
                },
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("No active Decision Gate cycle exists for `TP-92`.", response.json()["detail"])
        self.assertIn("!run TP-92", response.json()["detail"])
        oauth_client.update_issue_summary_and_description.assert_not_called()
