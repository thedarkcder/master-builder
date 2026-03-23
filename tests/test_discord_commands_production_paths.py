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
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Tenant
from orchestrator.tools.jira_oauth import JiraIssueBulkCreateResult, JiraIssueCreateResult

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
        self.create_calls: list[dict[str, object]] = []
        self.update_calls: list[dict[str, object]] = []

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


class DiscordCommandProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/discord_command_production.db"
        self.checkout_dir = os.path.join(self.temp_dir.name, "checkouts")

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = self.checkout_dir
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._seed_runtime_state()
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
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
            connection = JiraOAuthConnection(
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
                        "scope_in": ["Device relink decision"],
                        "scope_out": [],
                        "acceptance_criteria": ["Policy is documented"],
                        "how_to_test": ["Review the seeded issue"],
                        "nfr_intent": "MVP",
                        "dependencies": [],
                        "risks": [],
                        "labels": ["seeded"],
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

    def test_run_command_surfaces_decision_gate_questions_through_real_route(self) -> None:
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
        blocked_policy_payload = {
            "triggered": True,
            "reason": "Cross-account relink policy still needs clarification",
            "missing_sections": [],
            "questions": [
                "If a device_id is already linked to User A and then signs in as User B, should the relink be rejected or transferred?"
            ],
            "recommendation": "Decision required before build",
            "tags": ["[NEEDS-PM]"],
            "gtd_valid": True,
            "gtd_missing_criteria": [],
            "gtd_clarification_questions": [],
        }
        blocked_planner_payload = {
            "gate_status": "blocked_decision_gate",
            "reason": "Cross-account relink policy still needs clarification",
            "questions": [
                {
                    "question_id": "dg-1",
                    "kind": "decision_gate",
                    "question": "If a device_id is already linked to User A and then signs in as User B, should the relink be rejected or transferred?",
                    "status": "open",
                }
            ],
            "question_states": [
                {
                    "question_id": "dg-1",
                    "kind": "decision_gate",
                    "question": "If a device_id is already linked to User A and then signs in as User B, should the relink be rejected or transferred?",
                    "status": "open",
                }
            ],
            "resolved_items": [],
            "missing_items": [],
            "captured_answer_summary": "No decision answers are currently persisted for TP-42.",
        }

        with (
            patch("orchestrator.api.discord.ask.context.tenant_jira_oauth_context", return_value=fake_oauth),
            patch("orchestrator.api.discord.ask.context._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.discord.ask.context._jira_oauth_client", return_value=fake_jira_client),
            patch("orchestrator.core.precheck_policy.invoke_codex_json", return_value=blocked_policy_payload),
            patch("orchestrator.core.decision_planner.invoke_codex_json_with_tools", return_value=blocked_planner_payload),
        ):
            response = self._post_command("!run TP-42")

        self.assertEqual(response.status_code, 409)
        self.assertIn("Decision Gate still needs clarification for `TP-42`.", response.json()["detail"])
        self.assertIn("Cross-account relink policy", response.json()["detail"])

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
            patch("orchestrator.api.discord.ask.context._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.discord.ask.context._jira_oauth_client", return_value=fake_jira_client),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=fake_oauth),
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
            created_issue_keys=["TP-301"],
        )
        fake_oauth = {
            "client": fake_jira_client,
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
            "access_token": "access-token",
        }

        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=fake_oauth),
        ):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["command"], "issues")
        self.assertIn("TP-301", body["message"])
        self.assertEqual(body["data"]["created_issue_keys"], ["TP-301"])
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
            created_issue_keys=["TP-303"],
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
            tenant.policy_config = {**dict(tenant.policy_config or {}), "codex_model": "gpt-5.3-codex-spark"}
            project.policy_overrides = {**dict(project.policy_overrides or {}), "codex_model": "gpt-5.4"}
            session.commit()

        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=fake_oauth),
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
            tenant.policy_config = {**dict(tenant.policy_config or {}), "codex_model": "gpt-5.3-codex-spark"}
            project.policy_overrides = {**dict(project.policy_overrides or {}), "codex_model": "gpt-5.4"}
            session.commit()

        with (
            patch("orchestrator.api.discord.ingress.ask_runtime.build_codex_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ask.context.tenant_jira_oauth_context", return_value=fake_oauth),
            patch("orchestrator.api.discord.ask.context._refresh_jira_connection_tokens", return_value="access-token"),
            patch("orchestrator.api.discord.ask.context._jira_oauth_client", return_value=fake_jira_client),
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

    def test_issues_seed_retries_empty_codex_output_once_then_succeeds(self) -> None:
        runtime, queue = self._seed_runtime(
            [
                CodexRuntimeError(
                    "Codex CLI command failed (exit=1): Warning: no last agent message; wrote empty content to /tmp/seed.txt"
                ),
                self._planned_seed_output(summary="Retry succeeded for seed planner"),
            ]
        )
        fake_jira_client = _FakeJiraClient(
            issue_key="TP-42",
            summary="Cross-account relink policy",
            status="To Do",
            description="Clarify the device relink policy.",
            labels=["agent:ready"],
            created_issue_keys=["TP-302"],
        )
        fake_oauth = {
            "client": fake_jira_client,
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
            "access_token": "access-token",
        }

        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=fake_oauth),
        ):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertIn("TP-302", body["message"])
        self.assertEqual(body["data"]["created_issue_keys"], ["TP-302"])
        self.assertEqual(queue.calls, 2)

    def test_issues_seed_returns_controlled_503_when_codex_empty_output_repeats(self) -> None:
        runtime, queue = self._seed_runtime(
            [
                CodexRuntimeError(
                    "Codex CLI command failed (exit=1): Warning: no last agent message; wrote empty content to /tmp/seed.txt"
                ),
                CodexRuntimeError(
                    "Codex CLI command failed (exit=1): Warning: no last agent message; wrote empty content to /tmp/seed.txt"
                ),
            ]
        )

        with patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=runtime):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 503)
        self.assertIn("Codex issue seeding is unavailable", response.json()["detail"])
        self.assertIn("no last agent message", response.json()["detail"].lower())
        self.assertEqual(queue.calls, 2)

    def test_issues_seed_surfaces_usage_limit_without_retrying(self) -> None:
        runtime, queue = self._seed_runtime(
            [
                CodexRuntimeError(
                    "Codex CLI command failed (exit=1): You've hit your usage limit for GPT-5.3-Codex-Spark. Switch to another model now, or try again later."
                ),
            ]
        )

        with patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=runtime):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 503)
        self.assertIn("usage limit", response.json()["detail"].lower())
        self.assertIn("gpt-5.3-codex-spark", response.json()["detail"].lower())
        self.assertEqual(queue.calls, 1)

    def test_issues_seed_returns_controlled_error_for_incomplete_jira_context(self) -> None:
        runtime, queue = self._seed_runtime([self._planned_seed_output()])

        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=runtime),
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value={"client": None}),
        ):
            response = self._post_command("!issues seed draft a backlog item for relink policy")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Failed to seed Jira issues: Jira OAuth context is incomplete")
        self.assertEqual(queue.calls, 1)


if __name__ == "__main__":
    unittest.main()
