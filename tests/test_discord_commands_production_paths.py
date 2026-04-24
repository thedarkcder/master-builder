from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_state_machine import resolve_execution_gate_state
from orchestrator.core.decision_types import (
    DecisionClassification,
    IngressDecision,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.core.runtime_payload_models import AskIntentPayload
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import AtlassianOAuthConnection, Project, Tenant
from orchestrator.tools.atlassian_oauth import JiraIssueBulkCreateResult, JiraIssueCreateResult, JiraIssueDetail, JiraIssuePreview
from tests.test_support.db_harness import SqliteTemplateDbTestCase

pytestmark = pytest.mark.production_path


class _FakeJiraClient:
    def __init__(
        self,
        *,
        issue_key: str,
        summary: str,
        status: str,
        description: str,
        labels: list[str] | None = None,
        seed_existing_issues: list[SimpleNamespace] | None = None,
        created_issue_keys: list[str] | None = None,
    ) -> None:
        self._issue_key = issue_key
        self._summary = summary
        self._status = status
        self._description = description
        self._labels = list(labels or [])
        self._seed_existing_issues = list(seed_existing_issues or [])
        self._created_issue_keys = list(created_issue_keys or ["TP-301"])
        self._created_issue_index = 0
        self.create_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []
        self.link_calls: list[dict[str, object]] = []

    def search_issues_by_jql(self, *, access_token: str, cloud_id: str, jql: str, max_results: int):  # noqa: ARG002
        if "ORDER BY updated DESC" in jql or "issuekey in (" in jql:
            return list(self._seed_existing_issues)
        return [
            SimpleNamespace(
                key=self._issue_key,
                summary=self._summary,
                status=self._status,
            )
        ]

    def get_issue_detail(self, *, access_token: str, cloud_id: str, issue_id_or_key: str):  # noqa: ARG002
        return SimpleNamespace(
            key=issue_id_or_key,
            summary=self._summary,
            status=self._status,
            description=self._description,
            labels=list(self._labels),
        )

    def create_issues_bulk(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
        issues,
    ) -> JiraIssueBulkCreateResult:  # noqa: ANN001
        self.create_calls.append(
            {
                "access_token": access_token,
                "cloud_id": cloud_id,
                "project_key": project_key,
                "issues": list(issues),
            }
        )
        return JiraIssueBulkCreateResult(
            created=[
                JiraIssueCreateResult(key=issue_key, issue_id=str(index + 1))
                for index, issue_key in enumerate(self._created_issue_keys)
            ],
            errors=[],
        )

    def create_issue(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
        issue,
    ) -> JiraIssueCreateResult:  # noqa: ANN001
        self.create_calls.append(
            {
                "access_token": access_token,
                "cloud_id": cloud_id,
                "project_key": project_key,
                "issue": issue,
            }
        )
        if self._created_issue_index >= len(self._created_issue_keys):
            issue_key = f"{project_key}-{300 + self._created_issue_index + 1}"
        else:
            issue_key = self._created_issue_keys[self._created_issue_index]
        self._created_issue_index += 1
        return JiraIssueCreateResult(key=issue_key, issue_id=str(self._created_issue_index))

    def list_project_issue_types_for_create(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
    ) -> list[str]:  # noqa: ARG002
        return ["Epic", "Story", "Task", "Sub-task"]

    def update_issue_summary(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
    ) -> None:
        self.update_calls.append(
            {
                "access_token": access_token,
                "cloud_id": cloud_id,
                "issue_id_or_key": issue_id_or_key,
                "summary": summary,
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
        self.update_calls.append(
            {
                "access_token": access_token,
                "cloud_id": cloud_id,
                "issue_id_or_key": issue_id_or_key,
                "labels": list(labels),
            }
        )

    def update_issue_fields(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
        description: str,
        labels: list[str],
    ) -> None:
        self.update_calls.append(
            {
                "access_token": access_token,
                "cloud_id": cloud_id,
                "issue_id_or_key": issue_id_or_key,
                "summary": summary,
                "description": description,
                "labels": list(labels),
            }
        )

    def add_issue_link(
        self,
        *,
        access_token: str,
        cloud_id: str,
        inward_issue_key: str,
        outward_issue_key: str,
    ) -> None:
        self.link_calls.append(
            {
                "access_token": access_token,
                "cloud_id": cloud_id,
                "inward_issue_key": inward_issue_key,
                "outward_issue_key": outward_issue_key,
            }
        )


class _RuntimeQueue:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.calls = 0
        self.model_overrides: list[str | None] = []

    def __call__(
        self,
        _system: str,
        _user: str,
        _working_dir: str | None = None,
        _on_log_line=None,
        _reasoning_effort=None,
        _resume_session_id=None,
        _on_session_id=None,
        _on_usage=None,
        _model_override=None,
    ) -> str:
        self.calls += 1
        self.model_overrides.append(_model_override)
        if not self.outputs:
            return "{}"
        value = self.outputs.pop(0)
        if isinstance(value, Exception):
            raise value
        return str(value)


class DiscordCommandProductionPathTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = self._prepare_test_database(name_prefix="discord-command-production")
        self.checkout_dir = os.path.join(self.temp_dir.name, "checkouts")

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = self.checkout_dir
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"

        get_settings.cache_clear()
        reset_db_engine_cache()
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._seed_runtime_state()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
        self._cleanup_test_database()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        os.environ.pop("ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_USERNAME", None)
        os.environ.pop("ORCHESTRATOR_ADMIN_PASSWORD", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _seed_runtime_state(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="route25",
                name="Route25",
                is_enabled=True,
                jira_config={
                    "connection_id": "conn-1",
                    "project_keys": ["TP"],
                    "ready_statuses": ["To Do"],
                    "ready_jql": 'project = TP AND status = "To Do"',
                    "ready_label": "agent:ready",
                    "in_progress_label": "agent:in-progress",
                    "blocked_label": "agent:blocked",
                    "done_label": None,
                    "webhook_secret_ref": None,
                },
                github_config={"mode": "github_app", "installation_id": "12345"},
                repos_config={"github_repository": "https://github.com/example/repo"},
                policy_config={
                    "allow_jira_transitions": False,
                    "allow_pr_creation": True,
                    "allow_label_mutations": True,
                    "max_runtime_minutes": 30,
                    "max_dev_test_review_loops": 2,
                    "max_concurrent_runs": 2,
                    "allowed_commands": [],
                    "require_agents_md": False,
                },
                discord_config={
                    "channel_id": "discord-channel-1",
                    "notify_events": [],
                    "allowed_user_ids": ["u-1"],
                },
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="route25-default",
                tenant_id="route25",
                name="Route25 Default",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={
                    "channel_id": "discord-channel-1",
                    "notify_events": [],
                    "allowed_user_ids": ["u-1"],
                },
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            connection = AtlassianOAuthConnection(
                connection_id="conn-1",
                account_id="account-1",
                account_email="test@example.com",
                cloud_id="cloud-1",
                site_url="https://example.atlassian.net",
                scopes=["read:jira-work", "write:jira-work"],
                access_token_encrypted=encrypt_value(
                    plaintext="access-token",
                    encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                ),
                refresh_token_encrypted=encrypt_value(
                    plaintext="refresh-token",
                    encryption_key=os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"],
                ),
                access_token_expires_at=now + timedelta(hours=1),
                created_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, connection])
            session.commit()

    def _post_command(self, command: str, *, user_id: str = "u-1", channel_id: str = "discord-channel-1", command_params=None):
        payload = {
            "user_id": user_id,
            "channel_id": channel_id,
            "command": command,
        }
        if command_params is not None:
            payload["command_params"] = command_params
        return self.client.post("/discord/command/route25", json=payload)

    def _seed_runtime(self, outputs: list[object]) -> tuple[CodexRuntime, _RuntimeQueue]:
        queue = _RuntimeQueue(outputs)
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=4000,
            command="override",
            _request=queue,
        )
        return runtime, queue

    def _missing_ready_decision_result(self) -> DecisionEngineResult:
        pre_check = PreRunCheckResult(
            outcome="missing_ready_label",
            ready_label="agent:ready",
            ready_label_present=False,
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
        decision = IngressDecision(
            source="discord_run",
            pre_check=pre_check,
            block_reason="missing_ready_label",
            policy_error=None,
            guidance="Issue is missing the configured ready label. (agent:ready)",
            label_actions=(),
        )
        return DecisionEngineResult(
            decision=decision,
            issue_labels=[],
            classification=DecisionClassification.CLEAR,
            missing_slots=[],
            auto_resolved_slots=[],
            case_id="case-missing-ready",
            case_state="blocked",
            cycle_id=None,
            outbox_effect_ids=(),
            duplicate_event=False,
            execution_gate=resolve_execution_gate_state(
                decision=decision,
                classification=DecisionClassification.CLEAR,
            ),
        )

    def _planned_seed_output(
        self,
        *,
        summary: str = "Capture the relink policy",
        questions: list[str] | None = None,
    ) -> str:
        return json.dumps(
            {
                "project_key": "TP",
                "issues": [
                    {
                        "summary": summary,
                        "objective": "Clarify cross-account relink behavior.",
                        "user_value": "Support and engineering share one product-level relink decision.",
                        "recommendation": "Document the expected relink policy before implementation starts.",
                        "scope_in": ["Device relink decision"],
                        "scope_out": [],
                        "acceptance_criteria": ["Policy is documented"],
                        "ui_references": [],
                        "success_outcomes": ["Stakeholders can review the product behavior without technical detail."],
                        "dependencies": [],
                        "risks": [],
                        "open_questions": [],
                        "labels": ["seeded", "pm-parent"],
                        "issue_type": "Task",
                    }
                ],
                "questions": list(questions or []),
            }
        )

    def test_help_command_runs_real_command_route(self) -> None:
        response = self._post_command("!help")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["command"], "help")
        self.assertIn("!run", body["message"])

    def test_run_command_requires_ready_label_through_real_route(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-42", summary="Cross-account relink policy", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-42",
                    summary="Cross-account relink policy",
                    status="To Do",
                    description="Clarify the device relink policy.",
                    labels=[],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.executor._default_decision_clarification_port.evaluate_issue_clarification_state",
                return_value=self._missing_ready_decision_result(),
            ),
        ):
            response = self._post_command("!run TP-42")

        self.assertEqual(response.status_code, 409)
        self.assertIn("missing the configured ready label", response.json()["detail"])
        self.assertIn("agent:ready", response.json()["detail"])

    def test_reply_command_returns_controlled_message_when_no_active_cycle_exists(self) -> None:
        fake_jira_client = _FakeJiraClient(
            issue_key="TP-42",
            summary="Cross-account relink policy",
            status="To Do",
            description="Clarify the device relink policy.",
            labels=["agent:ready"],
        )
        fake_oauth = {
            "client": fake_jira_client,
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "access-token",
        }

        with (
            patch("orchestrator.api.discord.ask.context._refresh_atlassian_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.discord.ask.context._atlassian_oauth_client", return_value=fake_jira_client),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=fake_oauth),
        ):
            response = self._post_command("!reply TP-42 reject the relink")

        self.assertEqual(response.status_code, 409)
        self.assertIn("No active Decision Gate cycle exists for `TP-42`.", response.json()["detail"])

    def test_issues_seed_runs_real_planner_path_and_creates_issue(self) -> None:
        runtime, queue = self._seed_runtime(
            [self._planned_seed_output(summary="Capture the relink policy in backlog")]
        )
        fake_jira_client = _FakeJiraClient(
            issue_key="TP-42",
            summary="Cross-account relink policy",
            status="To Do",
            description="Clarify the device relink policy.",
            labels=["agent:ready"],
            created_issue_keys=["TP-301", "TP-302"],
        )
        fake_oauth = {
            "client": fake_jira_client,
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
            "access_token": "access-token",
        }

        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_issue_seed_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=fake_oauth),
        ):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["command"], "issues")
        self.assertIn("TP-301", body["message"])
        self.assertEqual(body["data"]["created_parent_issue_keys"], ["TP-301"])
        self.assertEqual(queue.calls, 1)
        self.assertEqual(len(fake_jira_client.create_calls), 1)
        self.assertEqual(fake_jira_client.create_calls[0]["project_key"], "TP")

    def test_issues_seed_uses_project_scoped_codex_model_override(self) -> None:
        runtime, queue = self._seed_runtime([self._planned_seed_output(summary="Use project model override")])
        fake_jira_client = _FakeJiraClient(
            issue_key="TP-42",
            summary="Cross-account relink policy",
            status="To Do",
            description="Clarify the device relink policy.",
            labels=["agent:ready"],
            created_issue_keys=["TP-303", "TP-304"],
        )
        fake_oauth = {
            "client": fake_jira_client,
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
            "access_token": "access-token",
        }
        with self.session_factory() as session:
            tenant = session.get(Tenant, "route25")
            project = session.get(Project, "route25-default")
            assert tenant is not None
            assert project is not None
            tenant.policy_config = {**dict(tenant.policy_config or {}), "codex_model": "gpt-5.4-mini"}
            project.policy_overrides = {**dict(project.policy_overrides or {}), "codex_model": "gpt-5.4"}
            session.commit()

        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_issue_seed_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value=fake_oauth),
        ):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(queue.model_overrides, ["gpt-5.4"])

    def test_ask_uses_project_scoped_codex_model_override(self) -> None:
        runtime, queue = self._seed_runtime([json.dumps({"message": "Scoped answer"})])
        fake_jira_client = _FakeJiraClient(
            issue_key="TP-42",
            summary="Cross-account relink policy",
            status="To Do",
            description="Clarify the device relink policy.",
            labels=["agent:ready"],
        )
        fake_oauth = SimpleNamespace(
            client=fake_jira_client,
            connection=SimpleNamespace(cloud_id="cloud-1"),
            access_token="access-token",
        )
        with self.session_factory() as session:
            tenant = session.get(Tenant, "route25")
            project = session.get(Project, "route25-default")
            assert tenant is not None
            assert project is not None
            tenant.policy_config = {**dict(tenant.policy_config or {}), "codex_model": "gpt-5.4-mini"}
            project.policy_overrides = {**dict(project.policy_overrides or {}), "codex_model": "gpt-5.4"}
            session.commit()

        with (
            patch("orchestrator.api.discord.commands.ask.build_runtime_for_selector", return_value=runtime),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="Scoped answer", command=None),
            ),
            patch("orchestrator.api.discord.ask.context.tenant_atlassian_oauth_context", return_value=fake_oauth),
            patch("orchestrator.api.discord.ask.context._refresh_atlassian_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.discord.ask.context._atlassian_oauth_client", return_value=fake_jira_client),
        ):
            response = self._post_command("!ask what is blocked?")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(queue.model_overrides, ["gpt-5.4"])

    def test_ask_requires_single_project_scope_when_unscoped(self) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Project(
                    project_id="route25-other",
                    tenant_id="route25",
                    name="Other Project",
                    github_repository="https://github.com/example/other",
                    jira_project_key="OTH",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={"channel_id": "discord-channel-2", "notify_events": []},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            tenant = session.get(Tenant, "route25")
            assert tenant is not None
            tenant.jira_config = {**dict(tenant.jira_config or {}), "project_keys": ["TP", "OTH"]}
            session.commit()

        response = self._post_command("!ask what is blocked?", channel_id=None)

        self.assertEqual(response.status_code, 409)
        self.assertIn("requires a single mapped project scope", response.json()["detail"])

    def test_issues_seed_returns_controlled_503_when_codex_output_is_empty(self) -> None:
        runtime, queue = self._seed_runtime(
            [
                CodexRuntimeError(
                    "Codex CLI command failed (exit=1): Warning: no last agent message; wrote empty content to /tmp/seed.txt"
                ),
            ]
        )
        with patch("orchestrator.api.discord.ingress.seed_runtime.build_issue_seed_runtime", return_value=runtime):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 503)
        self.assertIn("PM parent seeding runtime is unavailable", response.json()["detail"])
        self.assertIn("no last agent message", response.json()["detail"].lower())
        self.assertEqual(queue.calls, 1)

    def test_issues_seed_surfaces_usage_limit_without_retrying(self) -> None:
        runtime, queue = self._seed_runtime(
            [
                CodexRuntimeError(
                    "Codex CLI command failed (exit=1): You've hit your usage limit for GPT-5.4 Mini. Switch to another model now, or try again later."
                ),
            ]
        )

        with patch("orchestrator.api.discord.ingress.seed_runtime.build_issue_seed_runtime", return_value=runtime):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 503)
        self.assertIn("usage limit", response.json()["detail"].lower())
        self.assertIn("gpt-5.4 mini", response.json()["detail"].lower())
        self.assertEqual(queue.calls, 1)

    def test_issues_seed_returns_controlled_error_for_incomplete_jira_context(self) -> None:
        runtime, queue = self._seed_runtime([self._planned_seed_output()])

        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_issue_seed_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_atlassian_oauth_context", return_value={"client": None}),
        ):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Failed to seed Jira issues: Atlassian context is incomplete")
        self.assertEqual(queue.calls, 1)


if __name__ == "__main__":
    unittest.main()
