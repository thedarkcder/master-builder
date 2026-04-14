import hashlib
import hmac
import os
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi import HTTPException

from orchestrator.core.config import get_settings
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_state_machine import resolve_execution_gate_state
from orchestrator.core.decision_types import (
    DecisionClassification,
    DecisionEngineResult,
    IngressDecision,
    PrecheckOutcome,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.webhook_health import reset_webhook_health_tracker_for_tests
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from tests.test_support.db_harness import SqliteTemplateApiTestCase


class JiraWebhookHarness(SqliteTemplateApiTestCase):
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
