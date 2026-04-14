import asyncio
import os
import json
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select

from orchestrator.api.discord.interactions.followup import (
    _build_command_followup_message,
    _run_discord_command_followup,
    _send_discord_thread_followup,
)
from orchestrator.api.discord.interactions.parser import (
    _discord_issue_autocomplete_choices,
    _find_tenant_for_discord_channel,
    _parse_discord_interaction_command,
)
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext
from orchestrator.api.routes.webhook import (
    _parse_jira_comment_command,
)
from orchestrator.api.schemas import DiscordCommandResponse
from orchestrator.core.discord.channel_tenant_index import invalidate_discord_channel_tenant_index
from orchestrator.core.config import get_settings
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_types import (
    DecisionClassification,
    DecisionEngineResult,
    IngressDecision,
    PrecheckOutcome,
    resolve_execution_gate_state,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.precheck_question_lock import build_precheck_questions_block
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.core.webhook_health import reset_webhook_health_tracker_for_tests, webhook_health_tracker
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import FollowupContext, Project, Run, Tenant, WebhookJob
from orchestrator.tools.discord_api import DiscordApiError
from orchestrator.tools.jira_oauth import JiraIssueDetail, JiraIssuePreview, JiraOAuthError
from tests.test_support.db_harness import SqliteTemplateApiTestCase
from tests.workflow_test_support import add_run_with_workflow, make_run

pytestmark = pytest.mark.contract


class JiraWebhookTests(SqliteTemplateApiTestCase):
    _secrets_encryption_key: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        super().setUpClass()

    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {
            "ORCHESTRATOR_ADMIN_USERNAME": "admin",
            "ORCHESTRATOR_ADMIN_PASSWORD": "secret",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": cls._secrets_encryption_key,
            "ORCHESTRATOR_TEST_WEBHOOK_SECRET": "super-secret-token",
            "ORCHESTRATOR_TEST_GITHUB_WEBHOOK_SECRET": "github-super-secret-token",
        }

    @classmethod
    def bootstrap_template_state(cls) -> None:
        cls._create_tenant_with_client(
            client=cls._class_client,
            tenant_id="tenant-webhook",
        )

    @staticmethod
    def _pre_run_check(*, outcome: str = "ready_for_agent", capability: str = "linux") -> PreRunCheckResult:
        return PreRunCheckResult(
            outcome=outcome,
            ready_label="agent:ready",
            ready_label_present=True,
            required_worker_capability=capability,
            required_worker_label=f"worker:{capability}",
            required_worker_label_present=False,
            decision_gate=DecisionGateResult(
                triggered=(outcome == "decision_gate_required"),
                reason="Decision Gate not required" if outcome != "decision_gate_required" else "Missing GTD sections",
                missing_sections=(),
                questions=(),
                recommendation="Proceed" if outcome != "decision_gate_required" else "Decision required before build",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=(outcome != "gtd_required"),
                missing_criteria=(() if outcome != "gtd_required" else ("Dependencies and risks identified",)),
                clarification_questions=(
                    ()
                    if outcome != "gtd_required"
                    else ("Which dependencies or risks may impact delivery?",)
                ),
            ),
        )

    @staticmethod
    def _decision_result(
        *,
        pre_check: PreRunCheckResult,
        issue_labels: list[str],
        cycle_id: str | None = None,
        auto_resolved_slots: list[str] | None = None,
    ) -> DecisionEngineResult:
        parsed_outcome = PrecheckOutcome.parse(pre_check.outcome)
        block_reason = parsed_outcome.value if parsed_outcome in {
            PrecheckOutcome.DECISION_GATE_REQUIRED,
            PrecheckOutcome.GTD_REQUIRED,
            PrecheckOutcome.MISSING_READY_LABEL,
        } else None
        classification = (
            DecisionClassification.DECISION_GATE
            if parsed_outcome in {
                PrecheckOutcome.DECISION_GATE_REQUIRED,
                PrecheckOutcome.GTD_REQUIRED,
                PrecheckOutcome.EXECUTION_BLOCKED,
            }
            else DecisionClassification.CLEAR
        )
        decision = IngressDecision(
            source="jira_webhook",
            pre_check=pre_check,
            block_reason=block_reason,
            guidance=None,
            policy_error=None,
            label_actions=(),
        )
        return DecisionEngineResult(
            decision=decision,
            issue_labels=list(issue_labels),
            classification=classification,
            missing_slots=[],
            auto_resolved_slots=list(auto_resolved_slots or []),
            case_id="test-case",
            case_state="open",
            cycle_id=cycle_id,
            outbox_effect_ids=(),
            duplicate_event=False,
            execution_gate=resolve_execution_gate_state(decision=decision, classification=classification),
        )

    def setUp(self) -> None:
        self.database_url = self._start_test_database(name_prefix="webhook")
        self.webhook_secret_env = "ORCHESTRATOR_TEST_WEBHOOK_SECRET"
        self.webhook_secret_value = "super-secret-token"
        self.github_webhook_secret_env = "ORCHESTRATOR_TEST_GITHUB_WEBHOOK_SECRET"
        self.github_webhook_secret_value = "github-super-secret-token"

        get_settings.cache_clear()
        reset_db_engine_cache()
        invalidate_discord_channel_tenant_index()
        reset_webhook_health_tracker_for_tests()
        self.session_factory = create_session_factory(database_url=self.database_url)

        self._default_pre_run_check_patch = patch(
            "orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check",
            return_value=self._pre_run_check(),
        )
        self._default_pre_run_check_patch.start()
        self._default_precheck_decision_patch = patch(
            "orchestrator.api.webhooks.jira_admission_flow.evaluate_precheck_decision_with_labels",
            side_effect=self._evaluate_precheck_decision_with_labels,
        )
        self._default_precheck_decision_patch.start()

    def tearDown(self) -> None:
        self._cleanup_test_database()
        os.environ.pop(self.webhook_secret_env, None)
        os.environ.pop(self.github_webhook_secret_env, None)
        os.environ.pop("ORCHESTRATOR_GITHUB_WEBHOOK_SECRET_REF", None)
        os.environ.pop("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", None)
        get_settings.cache_clear()
        reset_db_engine_cache()
        invalidate_discord_channel_tenant_index()
        reset_webhook_health_tracker_for_tests()
        self._default_pre_run_check_patch.stop()
        self._default_precheck_decision_patch.stop()

    def _evaluate_precheck_decision_with_labels(
        self,
        *,
        context,
        session,
        settings,
        issue_description: str | None = None,
        idempotency_key: str | None = None,
    ):
        from orchestrator.api.webhooks import jira_admission_flow

        _ = idempotency_key
        effective_issue_description = context.issue_description if issue_description is None else issue_description
        pre_check = jira_admission_flow.evaluate_pre_run_check(
            tenant_id=context.tenant_id,
            project_id=context.project.project_id if context.project is not None else None,
            issue_key=context.issue_key,
            issue_summary=context.issue_summary,
            issue_description=effective_issue_description,
            issue_labels=context.issue_labels,
            ready_label=jira_admission_flow.resolve_ready_label_for_tenant(context.tenant),
        )
        labels_to_add = []
        if pre_check.ready_label and not pre_check.ready_label_present:
            labels_to_add.append(pre_check.ready_label)
        if pre_check.required_worker_label and not pre_check.required_worker_label_present:
            labels_to_add.append(pre_check.required_worker_label)
        if labels_to_add:
            try:
                oauth = jira_admission_flow.tenant_jira_oauth_context(
                    session=session,
                    tenant=context.tenant,
                    settings=settings,
                )
            except HTTPException:
                oauth = None
            if oauth is not None:
                oauth.client.add_issue_labels(
                    access_token=oauth.access_token,
                    cloud_id=oauth.connection.cloud_id,
                    issue_id_or_key=context.issue_key,
                    labels=labels_to_add,
                )
        issue_labels = [
            *list(context.issue_labels or []),
            *[label for label in labels_to_add if label not in set(context.issue_labels or [])],
        ]
        return self._decision_result(
            pre_check=pre_check,
            issue_labels=issue_labels,
            cycle_id=None,
        )

    def _create_tenant(
        self,
        tenant_id: str,
        webhook_secret_ref: str | None = None,
        github_webhook_secret_ref: str | None = None,
        github_installation_id: str = "12345",
        is_enabled: bool = True,
        max_concurrent_runs: int = 2,
        ready_trigger_mode: str = "status_recheck",
    ) -> None:
        self._create_tenant_with_client(
            client=self.client,
            tenant_id=tenant_id,
            webhook_secret_ref=webhook_secret_ref,
            github_webhook_secret_ref=github_webhook_secret_ref,
            github_installation_id=github_installation_id,
            is_enabled=is_enabled,
            max_concurrent_runs=max_concurrent_runs,
            ready_trigger_mode=ready_trigger_mode,
        )

    @staticmethod
    def _create_tenant_with_client(
        *,
        client,
        tenant_id: str,
        webhook_secret_ref: str | None = None,
        github_webhook_secret_ref: str | None = None,
        github_installation_id: str = "12345",
        is_enabled: bool = True,
        max_concurrent_runs: int = 2,
        ready_trigger_mode: str = "status_recheck",
    ) -> None:
        payload = {
            "name": tenant_id,
            "is_enabled": is_enabled,
            "jira": {
                "mcp_endpoint": "https://mcp.example.test",
                "project_keys": ["TP"],
                "ready_statuses": ["Ready for Agent"],
                "ready_trigger_mode": ready_trigger_mode,
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
        response = client.post("/api/admin/tenants", json=payload, auth=("admin", "secret"))
        if response.status_code != 201:
            raise AssertionError(f"Failed to create tenant `{tenant_id}`: {response.status_code} {response.text}")
        if response.json()["tenant_id"] != tenant_id:
            raise AssertionError(f"Unexpected tenant id for `{tenant_id}`: {response.json()['tenant_id']}")

    def _sign_github_payload(self, payload_bytes: bytes, secret: str) -> str:
        digest = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return f"sha256={digest}"

    def _jira_issue_payload(
        self,
        *,
        issue_key: str,
        labels: list[str] | None = None,
        status_name: str = "To Do",
        status_category_key: str = "indeterminate",
    ) -> dict:
        return {
            "webhookEvent": "jira:issue_updated",
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

    def _assert_jira_issue_event_queued(
        self,
        response,
        *,
        issue_key: str,
        reason: str = "queued_for_reconciliation",
        queued: bool = True,
    ) -> dict:
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertFalse(body["enqueued"])
        self.assertEqual(body["issue_key"], issue_key)
        self.assertEqual(body["reason"], reason)
        self.assertEqual(body["queued"], queued)
        return body

    def _process_one_webhook_job(self):
        with self.session_factory() as session:
            return process_next_webhook_job(
                session=session,
                settings=get_settings(),
                owner_id="worker:test",
            )

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
            patch("orchestrator.api.webhooks.jira_webhook_board_gate._fetch_issue_board_location", return_value=("backlog", None)),
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
            patch("orchestrator.api.webhooks.jira_webhook_board_gate._fetch_issue_board_location", return_value=("backlog", None)),
            patch("orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check", return_value=self._pre_run_check()),
            patch("orchestrator.core.discord.transport_executor.send_tenant_discord_message") as notify_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        body = self._assert_jira_issue_event_queued(response, issue_key="TP-139")
        self.assertEqual(body["webhook_event"], "issue_updated")
        notify_mock.assert_not_called()

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
            "orchestrator.api.webhooks.jira_admission_flow.tenant_jira_oauth_context",
            return_value=oauth_context,
        ), patch(
            "orchestrator.api.webhooks.jira_admission_flow.evaluate_pre_run_check",
            return_value=self._pre_run_check(capability="macos"),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-126")
        oauth_client.add_issue_labels.assert_not_called()

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
        with patch("orchestrator.api.webhooks.jira_webhook_board_gate._fetch_issue_board_location", return_value=("not_on_board", None)):
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
        with patch("orchestrator.api.webhooks.jira_webhook_board_gate._fetch_issue_board_location", return_value=("board", None)):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-123")

    def test_fetch_issue_board_location_still_checks_board_when_backlog_lookup_fails(self) -> None:
        from orchestrator.api.webhooks.jira_webhook_board_gate import _fetch_issue_board_location
        from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext

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
            JiraOAuthError("backlog endpoint unavailable"),
            {"issues": [{"key": "TP-123"}]},
        ]

        with (
            patch("orchestrator.api.webhooks.jira_webhook_board_gate.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_webhook_board_gate.JiraOAuthHttpClient", return_value=http_client),
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

    def test_webhook_marks_issue_created_backlog_ready_without_enqueue(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-130", status_name="Ready for Agent", labels=["agent:ready"])
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

    def test_webhook_transition_only_mode_ignores_status_recheck_without_transition(self) -> None:
        self._create_tenant("tenant-transition-only", ready_trigger_mode="transition_only")
        payload = self._jira_issue_payload(issue_key="TP-129", labels=["agent:ready"])

        response = self.client.post("/jira/webhook/tenant-transition-only", json=payload)

        self._assert_jira_issue_event_queued(response, issue_key="TP-129")

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

    def test_webhook_decision_reply_uses_captured_evidence_id_for_decision_event_idempotency(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-906", status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-2"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Decision reply content"}],
                    }
                ],
            },
        }
        decision_result = SimpleNamespace(
            classification=DecisionClassification.DECISION_GATE,
            cycle_id="cycle-1",
            decision=SimpleNamespace(
                pre_check=SimpleNamespace(
                    decision_gate=SimpleNamespace(
                        questions=("Need entitlement confirmation",),
                    )
                )
            ),
        )
        with (
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.active_case_and_cycle_for_issue",
                return_value=(SimpleNamespace(case_id="case-1"), SimpleNamespace(cycle_id="cycle-1")),
            ),
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.capture_decision_reply_and_recheck",
                return_value=SimpleNamespace(
                    evidence_id="evidence-jira-1",
                    decision_result=decision_result,
                ),
            ) as capture_mock,
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.load_cycle_question_feedback",
                return_value=({"question_id": "dg_1", "question_text": "Need entitlement confirmation"},),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-906")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        decision_event = capture_mock.call_args.kwargs["decision_event_factory"](
            SimpleNamespace(evidence_id="evidence-jira-1")
        )
        self.assertEqual(
            decision_event.idempotency_key,
            "decision-reply:evidence-jira-1",
        )

    def test_webhook_decision_reply_failure_handles_codex_runtime_error(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-907", status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-3"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Decision reply content"}],
                    }
                ],
            },
        }
        with (
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.active_case_and_cycle_for_issue",
                return_value=(SimpleNamespace(case_id="case-1"), SimpleNamespace(cycle_id="cycle-1")),
            ),
            patch(
                "orchestrator.api.webhooks.jira_webhook_comment_flow.capture_decision_reply_and_recheck",
                side_effect=CodexRuntimeError("bad structured output"),
            ),
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-907")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

    def test_webhook_pm_parent_material_change_refreshes_engineering_children(self) -> None:
        payload = self._jira_issue_payload(issue_key="TP-950", labels=["pm-parent"], status_name="To Do")
        payload["changelog"] = {"items": [{"field": "description", "fromString": "old", "toString": "new"}]}

        class _FakeClient:
            def search_issues_by_jql(self, **kwargs):  # noqa: ANN003
                jql = kwargs["jql"]
                if 'parent = "TP-950"' in jql or 'labels = "parent-tp-950"' in jql:
                    return [JiraIssuePreview(key="TP-951", summary="Update retry UI", status="To Do")]
                return []

            def get_issue_detail(self, **kwargs):  # noqa: ANN003
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
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_codex",
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
            def search_issues_by_jql(self, **kwargs):  # noqa: ANN003
                jql = kwargs["jql"]
                if 'parent = "TP-953"' in jql or 'labels = "parent-tp-953"' in jql:
                    return [JiraIssuePreview(key="TP-954", summary="Refresh retry UI", status="To Do")]
                return []

            def get_issue_detail(self, **kwargs):  # noqa: ANN003
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
        with (
            patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.webhooks.jira_parent_child_sync.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_codex",
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
            def get_issue_detail(self, **kwargs):  # noqa: ANN003
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
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_codex") as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-952")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        seed_mock.assert_not_called()

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
            patch("orchestrator.api.webhooks.jira_parent_child_sync.post_jira_comment", return_value=(True, None)) as comment_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-960")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(len(client.replaced_labels), 1)
        self.assertIn("sync-blocked", client.replaced_labels[0]["labels"])
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
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_codex",
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
                "orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_codex",
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
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "/mb run"}],
                    }
                ],
            },
        }

        run_plan = SimpleNamespace(content={"reason": "comment_command_run"}, actions=())
        with (
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow", return_value=run_plan) as run_flow_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_codex") as seed_mock,
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
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "/mb retry"}],
                    }
                ],
            },
        }

        run_plan = SimpleNamespace(content={"reason": "comment_command_retry"}, actions=())
        with (
            patch("orchestrator.api.webhooks.jira_application.plan_jira_run_flow", return_value=run_plan) as run_flow_mock,
            patch("orchestrator.api.webhooks.jira_parent_child_sync.seed_issues_with_codex") as seed_mock,
        ):
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-972")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        run_flow_mock.assert_called_once()
        seed_mock.assert_not_called()

    def test_webhook_project_not_mapped_does_not_process_decision_reply(self) -> None:
        payload = self._jira_issue_payload(issue_key="NOPE-1", status_name="To Do")
        payload["webhookEvent"] = "comment_created"
        payload["comment"] = {
            "author": {"accountId": "jira-user-4"},
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": "Decision reply content"}],
                    }
                ],
            },
        }
        with patch(
            "orchestrator.api.webhooks.jira_webhook_comment_flow.stage_handle_comment_decision_reply",
        ) as reply_stage_mock, patch(
            "orchestrator.api.webhooks.jira_admission_flow.evaluate_precheck_decision_with_labels",
        ) as evaluate_mock:
            response = self.client.post("/jira/webhook/tenant-webhook", json=payload)
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="NOPE-1")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        reply_stage_mock.assert_not_called()
        evaluate_mock.assert_not_called()

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
        self.assertIsNone(jobs[0].dedupe_key)

    def test_discord_component_interactions_are_queued_after_immediate_ack(self) -> None:
        payload = {
            "type": 3,
            "application_id": "discord-app-1",
            "token": "interaction-token-1",
            "channel_id": "discord-channel-1",
            "data": {"custom_id": "ask.approve.0123456789abcdef0123456789abcdef"},
            "member": {"user": {"id": "discord-user-1"}},
        }

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
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
        self.assertIsNone(jobs[0].dedupe_key)

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
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.discord.interactions.application._find_tenant_for_discord_channel", return_value=fake_tenant),
            patch("orchestrator.api.discord.interactions.application._discord_issue_autocomplete_choices", return_value=[]) as choices_mock,
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
            patch("orchestrator.api.discord.interactions.parser._project_filter_jql", return_value='project = "TP"'),
            patch("orchestrator.api.discord.interactions.parser._search_jira_issues_for_tenant", return_value=fake_issues),
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
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
            patch("orchestrator.api.discord.interactions.application._find_tenant_for_discord_channel", return_value=fake_tenant),
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
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
        ):
            response = self.client.post("/discord/interactions", json=payload)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], 9)
        self.assertEqual(body["data"]["custom_id"], "ask.reply.123456789012345678")
        self.assertEqual(body["data"]["components"][0]["components"][0]["custom_id"], "question")

    def test_discord_reply_modal_submit_is_queued_after_immediate_ack(self) -> None:
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

        with (
            patch("orchestrator.api.routes.webhook_discord_interactions._resolve_discord_interactions_public_key", return_value=b"\x01" * 32),
            patch("orchestrator.api.routes.webhook_discord_interactions._validate_discord_interaction_signature"),
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
        self.assertIsNone(jobs[0].dedupe_key)

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
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch("orchestrator.api.discord.interactions.followup._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup") as interaction_send_mock,
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
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="help",
                    message="ok",
                    data=None,
                ),
            ),
            patch(
                "orchestrator.api.discord.interactions.followup._send_discord_interaction_followup",
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
        from unittest.mock import create_autospec

        from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command

        command_executor = create_autospec(
            execute_tenant_discord_ingress_command,
            return_value=DiscordCommandResponse(ok=True, command="help", message="ok", data=None),
        )
        with (
            patch("orchestrator.api.discord.interactions.followup.execute_discord_ingress_command", command_executor) as command_mock,
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup"),
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
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
                return_value=DiscordCommandResponse(
                    ok=True,
                    command="ask",
                    message="Done.",
                    data={"issue_key": "TP-324"},
                ),
            ),
            patch("orchestrator.api.discord.interactions.followup._send_discord_ask_response_with_thread") as ask_thread_send_mock,
            patch("orchestrator.api.discord.interactions.followup._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup") as interaction_send_mock,
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
        interaction_send_mock.assert_called_once_with(
            application_id="discord-app-1",
            interaction_token="interaction-token-1",
            content="Posted response in a follow-up thread.",
            ephemeral=False,
            components=None,
            reply_to_message_id=None,
            channel_id="discord-channel-1",
        )

    def test_send_discord_thread_followup_falls_back_to_current_channel_when_thread_lookup_fails(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-webhook")
            self.assertIsNotNone(tenant)

            fake_client = MagicMock()
            fake_client.ensure_thread_for_message.side_effect = DiscordApiError("Cannot create nested thread")
            with (
                patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="bot-token"),
                patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=fake_client),
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
                patch("orchestrator.api.discord.interactions.followup.resolve_platform_secret_ref", return_value="bot-token"),
                patch("orchestrator.api.discord.interactions.followup_transport.DiscordApiClient", return_value=fake_client),
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
                "orchestrator.api.discord.interactions.followup.execute_discord_ingress_command",
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
            patch("orchestrator.api.discord.interactions.followup._send_discord_seed_followup_with_thread") as seed_thread_send_mock,
            patch("orchestrator.api.discord.interactions.followup._send_discord_thread_followup") as thread_send_mock,
            patch("orchestrator.api.discord.interactions.followup._send_discord_interaction_followup") as interaction_send_mock,
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
        interaction_send_mock.assert_called_once_with(
            application_id="discord-app-1",
            interaction_token="interaction-token-1",
            content="Posted response in a follow-up thread.",
            ephemeral=False,
            components=None,
            reply_to_message_id=None,
            channel_id="discord-channel-1",
        )

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
