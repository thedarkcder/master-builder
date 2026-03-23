from __future__ import annotations

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from orchestrator.cli import main as cli_main
from orchestrator.core.config import get_settings
from orchestrator.core.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import (
    DecisionAnswer,
    DecisionCase,
    DecisionCycle,
    DecisionEvidence,
    JiraOAuthConnection,
    Project,
    Tenant,
)


class AgentToolCliRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/agent_tool_cli.db"
        self.checkout_dir = os.path.join(self.temp_dir.name, "checkouts")

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ["ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR"] = self.checkout_dir

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._seed_runtime_state()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        os.environ.pop("ORCHESTRATOR_PROJECT_REPO_CHECKOUT_BASE_DIR", None)
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
                discord_config={"channel_id": "discord-channel-1", "notify_events": []},
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
                discord_config={"channel_id": "discord-channel-1", "notify_events": []},
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
            case = DecisionCase(
                case_id="case-1",
                tenant_id="route25",
                project_id="route25-default",
                issue_key="GP-124",
                state="blocked",
                blocked_reason="decision_gate_required",
                classification="decision_gate",
                issue_fingerprint="fingerprint-1",
                active_cycle_id="cycle-1",
                last_source="discord_run",
                last_event_type="discord_run",
                last_event_at=now,
                required_worker_capability="linux",
                required_worker_label="worker:linux",
                ready_label="agent:ready",
                ready_label_present=True,
                metadata_json={"source": "discord"},
                created_at=now,
                updated_at=now,
            )
            cycle = DecisionCycle(
                cycle_id="cycle-1",
                case_id="case-1",
                tenant_id="route25",
                project_id="route25-default",
                issue_key="GP-124",
                status="open",
                reason="Need one policy answer",
                classification="decision_gate",
                question_set_json=[
                    {
                        "id": "q1",
                        "kind": "decision_gate",
                        "text": "What is the cross-account relink policy?",
                    }
                ],
                unresolved_question_ids_json=["q1"],
                metadata_json={"asked_via": "discord"},
                opened_at=now,
                closed_at=None,
                created_at=now,
                updated_at=now,
            )
            answer = DecisionAnswer(
                answer_id="answer-1",
                case_id="case-1",
                cycle_id="cycle-1",
                tenant_id="route25",
                project_id="route25-default",
                issue_key="GP-124",
                question_id="q0",
                question_kind="decision_gate",
                question_text="What problem are we solving?",
                status="answered",
                normalized_answer="Prevent unsafe cross-account relinks.",
                source_transport="discord",
                source_ref="message-1",
                evidence_ids_json=["ev-1"],
                metadata_json={"notes": "Captured from Discord reply"},
                answered_at=now,
                accepted_at=None,
                created_at=now,
                updated_at=now,
            )
            evidence = DecisionEvidence(
                evidence_id="ev-1",
                dedupe_key="discord:cycle-1:message-1",
                case_id="case-1",
                cycle_id="cycle-1",
                tenant_id="route25",
                project_id="route25-default",
                issue_key="GP-124",
                source_transport="discord",
                source_ref="message-1",
                actor_ref="u-1",
                raw_text="Reject the relink when the device belongs to another user.",
                question_ids_json=["q1"],
                normalized_answers_json=[{"question_id": "q1", "answer": "reject"}],
                metadata_json={"source": "discord"},
                created_at=now,
            )
            session.add_all([tenant, project, connection, case, cycle, answer, evidence])
            session.commit()

    def _invoke_agent_tool(self, *, tool_name: str, args: dict[str, object], issue_key: str = "GP-124") -> tuple[int, dict]:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = cli_main(
                [
                    "agent-tool",
                    "--tenant",
                    "route25",
                    "--project",
                    "route25-default",
                    "--issue",
                    issue_key,
                    "--stage",
                    "decision_planner",
                    "--tool",
                    tool_name,
                    "--args",
                    json.dumps(args),
                ]
            )
        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        payload = json.loads(lines[-1])
        return exit_code, payload

    def test_cli_agent_tool_decision_read_state_returns_current_state(self) -> None:
        exit_code, payload = self._invoke_agent_tool(tool_name="decision.read_state", args={})

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        result = payload["result"]
        self.assertEqual(result["case"]["case_id"], "case-1")
        self.assertEqual(result["active_cycle"]["cycle_id"], "cycle-1")
        self.assertEqual(result["answers"][0]["question_id"], "q0")
        self.assertEqual(result["recent_evidence"][0]["evidence_id"], "ev-1")

    def test_cli_agent_tool_jira_get_issue_uses_real_cli_runtime(self) -> None:
        fake_client = SimpleNamespace(
            get_issue_detail=lambda **_: SimpleNamespace(
                key="GP-124",
                summary="Relink policy",
                status="To Do",
                description="Clarify the relink behavior.",
            )
        )

        with (
            patch("orchestrator.core.agent_tools.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.core.agent_tools.refresh_jira_connection_tokens", return_value="access-token"),
        ):
            exit_code, payload = self._invoke_agent_tool(
                tool_name="jira.get_issue",
                args={"issue_key": "GP-124"},
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["issue_key"], "GP-124")
        self.assertEqual(payload["result"]["summary"], "Relink policy")

    def test_cli_agent_tool_knowledge_read_succeeds_without_driver_errors(self) -> None:
        exit_code, payload = self._invoke_agent_tool(
            tool_name="knowledge.read",
            args={"query": "cross-account relink policy"},
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["result"]["query"], "cross-account relink policy")
        self.assertIn("text", payload["result"])
        self.assertIn("citations", payload["result"])


if __name__ == "__main__":
    unittest.main()
