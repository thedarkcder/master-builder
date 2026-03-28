import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from fastapi import HTTPException

from orchestrator.api.main import create_app
from orchestrator.api.discord.bug.service import build_discord_bug_description
from orchestrator.api.discord.ask.context import project_filter_jql
from orchestrator.api.discord.ingress.ask_runtime import ask_board_message, collect_ask_context, collect_github_ask_context
from orchestrator.api.discord.ingress.bug_runtime import create_discord_bug_issue
from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.discord.ingress.seed_runtime import build_seed_issue_description, seed_issues_with_codex
from orchestrator.api.discord.shared.state import store_seed_followup_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.core.decision_planner import DecisionPlannerQuestion, DecisionPlannerResult
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.pre_run_check import PreRunCheckResult
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.github_app import GitHubApiError
from orchestrator.tools.jira_oauth import (
    JiraIssueBulkCreateResult,
    JiraIssueCreateResult,
    JiraIssueDetail,
    JiraIssuePreview,
    JiraOAuthError,
)


pytestmark = pytest.mark.contract


class DiscordCommandApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/discord_commands_test.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self._project_checkout_patcher = patch(
            "orchestrator.api.admin.route_helpers.ensure_project_repository_checkout",
            return_value=None,
        )
        self._project_checkout_patcher.start()
        self.client = TestClient(create_app())
        self.session_factory = create_session_factory(database_url=self.database_url)

        create_response = self.client.post(
            "/api/admin/tenants",
            json={
                "name": "Discord Tenant",
                "is_enabled": True,
                "jira": {
                    "connection_id": None,
                    "project_keys": ["TP"],
                    "ready_statuses": ["To Do"],
                    "ready_jql": 'project = TP AND status = "To Do"',
                    "ready_label": "agent:ready",
                    "in_progress_label": "agent:in-progress",
                    "blocked_label": "agent:blocked",
                    "done_label": None,
                    "webhook_secret_ref": None,
                },
                "github": {
                    "mode": "github_app",
                    "webhook_secret_ref": None,
                    "installation_id": "12345",
                },
                "repos": {"github_repository": "https://github.com/example/repo"},
                "policy": {
                    "allow_jira_transitions": False,
                    "allow_pr_creation": True,
                    "allow_label_mutations": True,
                    "max_runtime_minutes": 30,
                    "max_dev_test_review_loops": 2,
                    "max_concurrent_runs": 2,
                    "allowed_commands": [],
                    "require_agents_md": False,
                },
                "discord": {
                    "channel_id": "discord-channel-1",
                    "notify_events": ["run_started"],
                    "allowed_user_ids": ["u-admin"],
                    "command_secret_ref": None,
                },
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_response.status_code, 201)
        self.tenant_id = create_response.json()["tenant_id"]
        self.default_project_id = f"{self.tenant_id}-default"

    def tearDown(self) -> None:
        self._project_checkout_patcher.stop()
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _queue_run(self, *, run_id: str, issue_key: str, status: str, project_id: str | None = None) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Run(
                    run_id=run_id,
                    tenant_id=self.tenant_id,
                    project_id=project_id or f"{self.tenant_id}-default",
                    issue_key=issue_key,
                    issue_summary=f"Issue {issue_key}",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    status=status,
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=now if status == "running" else None,
                    finished_at=None if status in {"queued", "running"} else now,
                )
            )
            session.commit()

    def _ready_precheck_result(self) -> PreRunCheckResult:
        return PreRunCheckResult(
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

    def _missing_ready_precheck_result(self) -> PreRunCheckResult:
        return PreRunCheckResult(
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

    def _create_project(self, *, project_id: str, jira_project_key: str, channel_id: str) -> None:
        with self.session_factory() as session:
            now = datetime.now(timezone.utc)
            session.add(
                Project(
                    project_id=project_id,
                    tenant_id=self.tenant_id,
                    name=project_id,
                    github_repository=f"https://github.com/example/{project_id}",
                    jira_project_key=jira_project_key,
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={"channel_id": channel_id, "notify_events": []},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_help_command_returns_supported_commands(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("!run", response.json()["message"])

    def test_status_command_includes_queue_and_active_runs(self) -> None:
        self._queue_run(run_id="run-queued-1", issue_key="TP-10", status="queued")
        self._queue_run(run_id="run-running-1", issue_key="TP-11", status="running")

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!status"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["queue_depth"], 1)
        self.assertEqual(len(response.json()["data"]["active_runs"]), 1)

    def test_sensitive_commands_require_allowlisted_user(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!run TP-99"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("allowlisted", response.json()["detail"])

    def test_thread_channel_is_allowed_when_registered_for_project(self) -> None:
        with self.session_factory() as session:
            project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-thread-1"]
            project.discord_config = discord_config
            session.commit()

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-thread-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])

    def test_non_registered_channel_is_rejected(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-other-1", "command": "!help"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("does not match", response.json()["detail"])

    def test_cancel_marks_run_as_cancelled(self) -> None:
        self._queue_run(run_id="run-cancel-me", issue_key="TP-12", status="queued")

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!cancel run-cancel-me"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["status"], "cancelled")

    def test_run_enqueues_when_jira_issue_is_executable(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
            return_value=JiraIssueDetail(
                key="TP-20",
                summary="Do thing",
                status="To Do",
                description="Objective: run command should carry Jira detail context.",
            ),
        ), patch(
            "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
            return_value=self._ready_precheck_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-20")
        run_id = response.json()["data"]["run_id"]
        with self.session_factory() as session:
            run = session.get(Run, run_id)
            assert run is not None
            self.assertEqual(run.issue_description, "Objective: run command should carry Jira detail context.")

    def test_run_precheck_uses_canonical_issue_description_without_board_context(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-20",
                    summary="Do thing",
                    status="To Do",
                    description="Objective: run command should carry Jira detail context.",
                    labels=["agent:ready", "worker:linux"],
                ),
            ),
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[
                    JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
                    JiraIssuePreview(key="TP-21", summary="Sibling ticket A", status="To Do"),
                    JiraIssuePreview(key="TP-22", summary="Sibling ticket B", status="Testing"),
                ],
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
                return_value=self._ready_precheck_result(),
            ) as precheck_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )

        self.assertEqual(response.status_code, 200)
        issue_description = str(precheck_mock.call_args.kwargs["issue_description"])
        self.assertIn("agent:ready", precheck_mock.call_args.kwargs["issue_labels"])
        self.assertEqual(issue_description, "Objective: run command should carry Jira detail context.")

    def test_run_rejects_when_ready_label_is_missing(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
                return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
                return_value=JiraIssueDetail(
                    key="TP-20",
                    summary="Do thing",
                    status="To Do",
                    description="Objective: run command should carry Jira detail context.",
                    labels=[],
                ),
            ),
            patch(
                "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
                return_value=self._missing_ready_precheck_result(),
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("missing the configured ready label", response.json()["detail"])
        self.assertIn("agent:ready", response.json()["detail"])

    def test_run_conflict_includes_active_run_details(self) -> None:
        self._queue_run(run_id="run-active-1", issue_key="TP-20", status="running")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
            return_value=self._ready_precheck_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("run_already_active", response.json()["detail"])
        self.assertIn("run-active-1", response.json()["detail"])
        self.assertIn("running", response.json()["detail"])

    def test_retry_enqueues_from_latest_failed_run(self) -> None:
        self._queue_run(run_id="run-failed-1", issue_key="TP-30", status="failed")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
            return_value=JiraIssueDetail(
                key="TP-30",
                summary="Retry thing",
                status="To Do",
                description="Objective: refreshed from Jira for retry.",
            ),
        ), patch(
            "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
            return_value=self._ready_precheck_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-30")
        retry_run_id = response.json()["data"]["run_id"]
        with self.session_factory() as session:
            retry_run = session.get(Run, retry_run_id)
            assert retry_run is not None
            self.assertEqual(retry_run.issue_description, "Objective: refreshed from Jira for retry.")

    def test_retry_allows_in_progress_issue_status(self) -> None:
        self._queue_run(run_id="run-failed-2", issue_key="TP-31", status="failed")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-31", summary="Retry thing", status="In Progress"),
        ), patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_detail",
            return_value=JiraIssueDetail(
                key="TP-31",
                summary="Retry thing",
                status="In Progress",
                description="Objective: refreshed from Jira for retry.",
            ),
        ), patch(
            "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
            return_value=self._ready_precheck_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-2"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-31")

    def test_retry_conflict_includes_active_run_details(self) -> None:
        self._queue_run(run_id="run-failed-1", issue_key="TP-30", status="failed")
        self._queue_run(run_id="run-active-2", issue_key="TP-30", status="queued")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ), patch(
            "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
            return_value=self._ready_precheck_result(),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-1"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("run_already_active", response.json()["detail"])
        self.assertIn("run-active-2", response.json()["detail"])

    def test_reply_updates_jira_from_dict_oauth_context_and_enqueues_retry(self) -> None:
        self._queue_run(run_id="run-failed-reply-1", issue_key="TP-88", status="failed")
        oauth_client = SimpleNamespace(
            get_issue_detail=unittest.mock.MagicMock(
                return_value=SimpleNamespace(
                    summary="Old summary",
                    description="Objective: old",
                )
            ),
            update_issue_summary_and_description=unittest.mock.MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        runtime = unittest.mock.MagicMock()
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
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=oauth_context),
            patch("orchestrator.api.discord.ingress.executor.build_codex_runtime", return_value=runtime),
            patch(
                "orchestrator.api.discord.commands.run_controls.capture_decision_reply_and_recheck",
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

        with patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value={"access_token": "tok-only"}):
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
            get_issue_detail=unittest.mock.MagicMock(
                return_value=SimpleNamespace(
                    summary="Old summary",
                    description="Objective: old",
                )
            ),
            update_issue_summary_and_description=unittest.mock.MagicMock(),
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
            patch(
                "orchestrator.api.discord.commands.run_controls.dispatch_run_control_command"
            ) as dispatch_mock,
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview"
            ) as preview_mock,
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.commands.run_controls.capture_decision_reply_and_recheck",
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
            patch("orchestrator.core.decision_engine.plan_decision_questions", return_value=planner_result),
            patch(
                "orchestrator.api.discord.commands.run_controls.unresolved_question_feedback_for_cycle",
                return_value=[
                    {
                        "question_id": "dg_1",
                        "question_text": "What entitlement/capability values are required for production and staging?",
                        "note": "Config values were captured, but entitlement confirmation is still missing.",
                        "status": "open",
                    }
                ],
            ),
            patch("orchestrator.api.discord.commands.run_controls.build_precheck_message") as build_message_mock,
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

    def test_reply_uses_captured_evidence_id_for_decision_event_idempotency(self) -> None:
        self._queue_run(run_id="run-failed-reply-idempotency", issue_key="TP-90", status="failed")
        oauth_client = SimpleNamespace(
            get_issue_detail=unittest.mock.MagicMock(
                return_value=SimpleNamespace(
                    summary="Old summary",
                    description="Objective: old",
                    labels=["agent:ready"],
                )
            ),
            add_issue_comment=unittest.mock.MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }
        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.commands.run_controls.capture_decision_reply_and_recheck"
            ) as reply_recheck_mock,
            patch(
                "orchestrator.api.discord.commands.run_controls.unresolved_question_feedback_for_cycle",
                return_value=[
                    {
                        "question_id": "dg_1",
                        "question_text": "What entitlement/capability values are required for production and staging?",
                        "note": "Config values were captured, but entitlement confirmation is still missing.",
                        "status": "open",
                    }
                ],
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
        self.assertEqual(
            decision_event.idempotency_key,
            "decision-reply:evidence-123",
        )

    def test_reply_when_only_gtd_is_blocking_does_not_surface_decision_gate_reason(self) -> None:
        self._queue_run(run_id="run-failed-reply-gtd", issue_key="TP-90", status="failed")
        oauth_client = SimpleNamespace(
            get_issue_detail=unittest.mock.MagicMock(
                return_value=SimpleNamespace(
                    summary="Old summary",
                    description="Objective: old",
                    labels=["agent:ready"],
                )
            ),
            update_issue_summary_and_description=unittest.mock.MagicMock(),
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
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.commands.run_controls.capture_decision_reply_and_recheck",
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
            patch("orchestrator.core.decision_engine.plan_decision_questions", return_value=planner_result),
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

    def test_reply_without_retryable_run_queues_initial_run_after_clarification(self) -> None:
        oauth_client = SimpleNamespace(
            get_issue_detail=unittest.mock.MagicMock(
                return_value=SimpleNamespace(
                    summary="Old summary",
                    description="Objective: old",
                    labels=["worker:linux"],
                )
            ),
            update_issue_summary_and_description=unittest.mock.MagicMock(),
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
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.commands.run_controls.capture_decision_reply_and_recheck",
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
                "orchestrator.api.discord.commands.run_controls.evaluate_execution_readiness_only",
                wraps=__import__(
                    "orchestrator.api.discord.commands.run_controls",
                    fromlist=["evaluate_execution_readiness_only"],
                ).evaluate_execution_readiness_only,
            ) as readiness_mock,
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
            readiness_mock.call_args.kwargs["issue_labels"],
            ["worker:linux", "agent:ready"],
        )

    def test_reply_without_active_decision_cycle_returns_explicit_conflict(self) -> None:
        oauth_client = SimpleNamespace(
            get_issue_detail=unittest.mock.MagicMock(
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
            update_issue_summary_and_description=unittest.mock.MagicMock(),
        )
        oauth_context = {
            "connection": SimpleNamespace(cloud_id="cloud-1"),
            "access_token": "tok-1",
            "client": oauth_client,
        }

        with (
            patch("orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context", return_value=oauth_context),
            patch(
                "orchestrator.api.discord.commands.run_controls.capture_decision_reply_and_recheck",
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

    def test_link_rejects_issue_outside_mapped_project_scope(self) -> None:
        self._create_project(project_id=f"{self.tenant_id}-other", jira_project_key="OTH", channel_id="discord-other-1")
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-other-1", "command": "!link TP-20"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_run_rejects_issue_outside_mapped_project_scope(self) -> None:
        self._create_project(project_id=f"{self.tenant_id}-other", jira_project_key="OTH", channel_id="discord-other-1")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-other-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_retry_rejects_run_outside_mapped_project_scope(self) -> None:
        self._create_project(project_id=f"{self.tenant_id}-other", jira_project_key="OTH", channel_id="discord-other-1")
        self._queue_run(run_id="run-tp-1", issue_key="TP-30", status="failed", project_id=f"{self.tenant_id}-default")
        with patch(
            "orchestrator.api.discord.ingress.jira_runtime.fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-other-1", "command": "!retry run-tp-1"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_cancel_rejects_run_outside_mapped_project_scope(self) -> None:
        self._create_project(project_id=f"{self.tenant_id}-other", jira_project_key="OTH", channel_id="discord-other-1")
        self._queue_run(run_id="run-tp-cancel", issue_key="TP-31", status="queued", project_id=f"{self.tenant_id}-default")
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-admin", "channel_id": "discord-other-1", "command": "!cancel run-tp-cancel"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("outside the mapped project scope", response.json()["detail"])

    def test_ask_command_requires_question(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !ask", response.json()["detail"])

    def test_pm_command_requires_question(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!pm"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !pm", response.json()["detail"])

    def test_pm_command_returns_product_first_answer_and_stores_minimal_history(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What kind of rollout narrative do you need?\n"
                        "Examples: executive launch summary, customer-facing changelog, or support handoff."
                    ),
                    "brief": {
                        "objective": "Improve checkout recovery",
                        "user_value": "Customers recover from checkout failures more clearly.",
                        "target_user": "",
                        "primary_journey": "",
                        "acceptance_criteria": [],
                        "ui_references": [],
                        "constraints": [],
                        "success_outcomes": [],
                        "recommendation": "Ship in one sprint",
                        "scope_in": ["Retry flow"],
                        "scope_out": ["Payments provider migration"],
                        "risks": ["Missing telemetry"],
                        "open_questions": ["Fallback copy approval"],
                        "next_steps": ["Clarify the intended rollout audience."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_codex",
                side_effect=AssertionError("Incomplete PM interview should not seed Jira"),
            ) as seed_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm shape a rollout narrative for TP-20",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertIn("Examples:", command_response.message)
        self.assertTrue(command_response.data["pm_mode"])
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("product_brief_markdown", command_response.data)
        self.assertNotIn("parent_issue_key", command_response.data)
        self.assertFalse(command_response.data["ready_to_write"])
        seed_mock.assert_not_called()
        self.assertEqual(plan_mock.call_args.kwargs["request_text"], "shape a rollout narrative for TP-20")

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("pm "))
            self.assertIn("Examples:", str(latest_entry.get("answer") or ""))

    def test_pm_command_creates_parent_issue_and_returns_parent_metadata(self) -> None:
        planning_result = SimpleNamespace(
            planning_state="planning_completed",
            required_tasks=("Implement retry telemetry", "Add fallback UX validation"),
            findings=("Telemetry coverage must be explicit.",),
            recommendations=("Keep the first cut focused on customer-visible recovery.",),
            acceptance_impacts=("Acceptance criteria must mention fallback UX.",),
            open_behavior_questions=(),
            stages=(
                SimpleNamespace(
                    planning_state="engineering_planning",
                    to_payload=lambda: {
                        "findings": ["Split telemetry and UX work."],
                        "recommendations": ["One child ticket per implementation slice."],
                        "required_tasks": ["Implement retry telemetry"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Telemetry needs explicit coverage."],
                    },
                ),
                SimpleNamespace(
                    planning_state="security_planning",
                    to_payload=lambda: {
                        "findings": ["Protect retry events from abuse."],
                        "recommendations": ["Add misuse checks."],
                        "required_tasks": ["Add fallback UX validation"],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Security validation is required."],
                    },
                ),
                SimpleNamespace(
                    planning_state="test_planning",
                    to_payload=lambda: {
                        "findings": ["Regression coverage is required."],
                        "recommendations": ["Automate the failure-recovery path."],
                        "required_tasks": [],
                        "open_behavior_questions": [],
                        "acceptance_impacts": ["Tests should cover visible recovery."],
                    },
                ),
            ),
        )
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": "The PM brief is complete and ready for parent creation.",
                    "brief": {
                        "objective": "Ship checkout recovery",
                        "user_value": "Customers recover cleanly from checkout failures.",
                        "target_user": "Customers experiencing checkout failure",
                        "primary_journey": "Retry after a failed checkout",
                        "acceptance_criteria": [
                            "Customers can retry checkout from the failure state",
                            "Fallback UX explains what to do next",
                        ],
                        "ui_references": ["Checkout failure screen"],
                        "constraints": ["Use the existing checkout system"],
                        "success_outcomes": ["Higher recovery rate from checkout failures"],
                        "recommendation": "Focus on the customer-visible fallback first.",
                        "scope_in": ["Retry telemetry", "Fallback UX"],
                        "scope_out": ["Provider migration"],
                        "risks": ["Analytics gap"],
                        "open_questions": [],
                        "next_steps": ["Review the parent feature with product"],
                    },
                    "status": "ready_to_write",
                    "ready_to_write": True,
                },
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_codex",
                return_value=(
                    "PM parent issue upsert complete. Created 1: TP-501. Updated 0: none.",
                    {
                        "created_parent_issue_keys": ["TP-501"],
                        "updated_parent_issue_keys": [],
                        "created_parent_issue_links": ["https://example.atlassian.net/browse/TP-501"],
                        "updated_parent_issue_links": [],
                        "all_parent_issue_keys": ["TP-501"],
                    },
                ),
            ) as seed_mock,
            patch(
                "orchestrator.api.discord.commands.ask.run_specialist_planning_fanout",
                return_value=planning_result,
            ) as planning_mock,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_issues_with_codex",
                return_value=(
                    "Issue upsert complete. Parent: TP-501. Created 2: TP-502, TP-503.",
                    {
                        "parent_issue_key": "TP-501",
                        "created_children": ["TP-502", "TP-503"],
                        "updated_children": [],
                        "children_sync_status": "children_current",
                    },
                ),
            ) as seed_children_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm final handoff for TP-20 checkout reliability",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertEqual(command_response.data["parent_issue_key"], "TP-501")
        self.assertEqual(command_response.data["created_parent_issue_keys"], ["TP-501"])
        self.assertEqual(command_response.data["created_children"], ["TP-502", "TP-503"])
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertTrue(command_response.data["ready_to_write"])
        self.assertIn("## Approved Product Brief", str(command_response.data["product_brief_markdown"]))
        self.assertIn("PM parent issue upsert complete", command_response.message)
        self.assertIn("Issue upsert complete", command_response.message)
        self.assertEqual(plan_mock.call_args.kwargs["request_text"], "final handoff for TP-20 checkout reliability")
        seed_mock.assert_called_once()
        planning_mock.assert_called_once()
        seed_children_mock.assert_called_once()

    def test_pm_approve_is_rejected(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!pm approve rollout to beta"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !pm <product request>", response.json()["detail"])

    def test_engineer_command_returns_advisory_persona_response(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=("TP-20", "To Do", [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.personas.answer_voice_room_persona_with_codex",
                return_value={
                    "message": "Split the work by persistence, API, and validation boundaries.",
                    "brief": {"focus": "decomposition"},
                },
            ) as answer_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!engineer @TP-20 how should we split this?",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "engineer")
        self.assertTrue(command_response.data["advisory_only"])
        self.assertEqual(command_response.data["persona_id"], "engineer")
        self.assertEqual(command_response.data["issue_key"], "TP-20")
        self.assertIn("persistence", command_response.message.lower())
        answer_mock.assert_called_once()

    def test_tester_command_maps_to_qa_persona(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.personas.answer_voice_room_persona_with_codex",
                return_value={
                    "message": "Cover the happy path and one failed validation path.",
                    "brief": {},
                },
            ) as answer_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!tester what should we verify?",
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "tester")
        self.assertEqual(command_response.data["persona_id"], "qa")
        self.assertEqual(answer_mock.call_args.kwargs["persona_id"], "qa")

    def test_pm_room_mode_routes_to_persona_room_runtime(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What kind of PM outcome do you need here?\n"
                        "Examples: customer-facing feature brief, internal product spec, or rollout plan."
                    ),
                    "brief": {
                        "objective": "Shape the MVP for transcription and routing.",
                        "user_value": "Stakeholders can align on the first release.",
                        "recommendation": "Clarify the intended PM artifact first.",
                        "scope_in": ["Transcription", "Routing"],
                        "scope_out": ["Full architecture design"],
                        "risks": ["The product brief is still too broad."],
                        "open_questions": ["What decision should the PM help make next?"],
                        "next_steps": ["Continue the PM interview in the room thread."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm how should this fit together",
                    command_params={"room_mode": "true"},
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertTrue(command_response.data["room_mode"])
        self.assertTrue(command_response.data["voice_mode"])
        self.assertEqual(command_response.data["persona_id"], "pm")
        self.assertEqual(command_response.data["persona_name"], "PM")
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("Examples:", command_response.message)
        plan_mock.assert_called_once()

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("room "))
            self.assertTrue(str(latest_entry.get("answer") or "").startswith("pm: "))

    def test_pm_voice_mode_routes_to_persona_runtime_with_channel_local_history(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, [{"question": "voice earlier", "answer": "security: older reply"}]),
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_pm_interview_with_codex",
                return_value={
                    "message": (
                        "What product decision do you need to make about session security?\n"
                        "Examples: customer-facing requirement, scope boundary, or rollout constraint."
                    ),
                    "brief": {
                        "objective": "Clarify the product requirement for session security.",
                        "user_value": "Stakeholders understand the expected security behavior.",
                        "recommendation": "Stay product-level until the PM brief is complete.",
                        "scope_in": ["Session security requirement"],
                        "scope_out": ["Detailed security design"],
                        "risks": ["The ask mixes product and implementation concerns."],
                        "open_questions": ["Which user-visible behavior matters most?"],
                        "next_steps": ["Continue the PM interview with one focused answer."],
                    },
                    "status": "question_pending",
                    "ready_to_write": False,
                },
            ) as plan_mock,
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!pm what should we do about session security",
                    command_params={"voice_mode": "true"},
                ),
                session=session,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "pm")
        self.assertFalse(command_response.data["room_mode"])
        self.assertTrue(command_response.data["voice_mode"])
        self.assertEqual(command_response.data["persona_id"], "pm")
        self.assertEqual(command_response.data["persona_name"], "PM")
        self.assertEqual(command_response.data["followup_context_type"], "pm_interview")
        self.assertIn("Examples:", command_response.message)
        plan_mock.assert_called_once()
        self.assertEqual(plan_mock.call_args.kwargs["history"], [{"question": "voice earlier", "answer": "security: older reply"}])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history = list((tenant.discord_config or {}).get("ask_history") or [])
            self.assertTrue(history)
            latest_entry = history[-1]
            self.assertTrue(str(latest_entry.get("question") or "").startswith("voice "))
            self.assertTrue(str(latest_entry.get("answer") or "").startswith("pm: "))

    def test_ask_command_returns_board_answer(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"To Do": 2}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_codex",
                return_value="Board snapshot",
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!ask what is on the board",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["message"], "Board snapshot")
        self.assertEqual(response.json()["data"]["status_counts"]["To Do"], 2)

    def test_ask_command_supports_issue_scope(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=("TP-101", None, [], {}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_codex",
                return_value="Issue snapshot",
            ) as answer_mock,
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!ask @TP-101 summarize status",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["message"], "Issue snapshot")
        self.assertEqual(response.json()["data"]["issue_key"], "TP-101")
        self.assertEqual(answer_mock.call_args.kwargs["invocation_context"].issue_key, "TP-101")

    def test_ask_command_rejects_invalid_issue_scope_token(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask @bad summarize"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !ask @ISSUE-123", response.json()["detail"])

    def test_project_filter_jql_scopes_to_channel_project(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)

            default_project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(default_project)
            default_discord = dict(default_project.discord_config or {})
            default_discord["channel_id"] = "discord-channel-1"
            default_project.discord_config = default_discord

            other_project = session.get(Project, other_project_id)
            self.assertIsNotNone(other_project)
            other_discord = dict(other_project.discord_config or {})
            other_discord["channel_id"] = "discord-channel-2"
            other_project.discord_config = other_discord
            session.commit()

            self.assertEqual(
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-channel-1"),
                'project = "TP"',
            )
            self.assertEqual(
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-channel-2"),
                'project = "OTH"',
            )
            with self.assertRaises(HTTPException):
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-unmapped")
            self.assertIn(
                project_filter_jql(session=session, tenant=tenant, channel_id="__dm__"),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )
            self.assertIn(
                project_filter_jql(session=session, tenant=tenant, channel_id="dm"),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )
            self.assertIn(
                project_filter_jql(session=session, tenant=tenant),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )

    def test_project_filter_jql_excludes_archived_projects_from_unscoped_queries(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Archived Project",
                "github_repository": "https://github.com/example/archived",
                "jira_project_key": "ARC",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        archived_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            archived_project = session.get(Project, archived_project_id)
            self.assertIsNotNone(archived_project)
            archived_project.is_archived = True
            session.commit()

            jql = project_filter_jql(session=session, tenant=tenant, channel_id="dm")
            self.assertEqual(jql, 'project = "TP"')
            self.assertNotIn("ARC", jql)

    def test_collect_ask_context_scopes_jql_to_channel_project(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            other_project = session.get(Project, other_project_id)
            self.assertIsNotNone(other_project)
            other_discord = dict(other_project.discord_config or {})
            other_discord["channel_id"] = "discord-channel-2"
            other_project.discord_config = other_discord
            session.commit()

            with patch("orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant", return_value=[]) as search_mock:
                collect_ask_context(
                    session=session,
                    tenant=tenant,
                    channel_id="discord-channel-2",
                    question="what changed",
                )

        called_jql = search_mock.call_args.kwargs["jql"]
        self.assertIn('project = "OTH"', called_jql)
        self.assertNotIn('project = "TP"', called_jql)

    def test_ask_board_message_passes_channel_scoped_project_key_to_codex(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)

            default_project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(default_project)
            default_discord = dict(default_project.discord_config or {})
            default_discord["channel_id"] = "discord-channel-1"
            default_project.discord_config = default_discord

            other_project = session.get(Project, other_project_id)
            self.assertIsNotNone(other_project)
            other_discord = dict(other_project.discord_config or {})
            other_discord["channel_id"] = "discord-channel-2"
            other_project.discord_config = other_discord
            session.commit()

            with (
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                    return_value=(
                        None,
                        None,
                        [{"key": "OTH-1", "summary": "Other item", "status": "To Do"}],
                        {"To Do": 1},
                        [],
                    ),
                ),
                patch("orchestrator.api.discord.ingress.ask_runtime.build_codex_runtime"),
                patch("orchestrator.api.discord.ingress.ask_runtime.answer_board_question_with_codex", return_value="Board answer") as answer_mock,
            ):
                message, _ = ask_board_message(
                    session=session,
                    tenant=tenant,
                    user_id="u-viewer",
                    channel_id="discord-channel-2",
                    question="what is in progress",
                )

        self.assertEqual(message, "Board answer")
        self.assertEqual(answer_mock.call_args.kwargs["project_keys"], ["OTH"])

    def test_project_filter_jql_scopes_to_project_thread_channel(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Thread Project",
                "github_repository": "https://github.com/example/thread",
                "jira_project_key": "THR",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            project = session.get(Project, project_id)
            self.assertIsNotNone(project)
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-project-thread-1"]
            project.discord_config = discord_config
            session.commit()

            self.assertEqual(
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-project-thread-1"),
                'project = "THR"',
            )

    def test_archived_project_channel_is_rejected(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Archived Project",
                "github_repository": "https://github.com/example/archived",
                "jira_project_key": "ARC",
                "discord": {"channel_id": "discord-archived-project", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        project_id = create_project.json()["project_id"]
        archive_response = self.client.put(
            f"/api/admin/tenants/{self.tenant_id}/projects/{project_id}",
            json={
                "name": "Archived Project",
                "github_repository": "https://github.com/example/archived",
                "jira_project_key": "ARC",
                "policy_overrides": {},
                "environment": {},
                "secret_refs": {},
                "discord": {"channel_id": "discord-archived-project", "notify_events": []},
                "is_archived": True,
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(archive_response.status_code, 200)

        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-archived-project", "command": "!help"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("does not match", response.json()["detail"])

    def test_ask_command_requires_confirmation_when_intent_is_action(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.build_codex_runtime",
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "command", "summary": "Queue the issue run now", "command": "!run TP-20"},
            ),
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask please run TP-20",
                ),
                session=session,
                require_ask_confirmation=True,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "ask")
        self.assertIsInstance(command_response.data, dict)
        self.assertTrue(command_response.data["requires_confirmation"])
        self.assertEqual(command_response.data["proposed_command"], "!run TP-20")
        self.assertTrue(command_response.data["request_id"])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            pending = tenant.discord_config.get("pending_ask_actions", [])
            self.assertEqual(len(pending), 1)

    def test_ask_command_confirmation_uses_channel_scoped_project_keys(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
                "discord": {"channel_id": "discord-channel-2", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "OTH-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "Board answer"},
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_codex",
                return_value="Scoped board answer",
            ),
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-2",
                    command="!ask what is blocked?",
                ),
                session=session,
                require_ask_confirmation=True,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(command_response.command, "ask")
        self.assertEqual(plan_mock.call_args.kwargs["project_keys"], ["OTH"])

    def test_ask_confirmation_passes_github_context_to_intent_planner(self) -> None:
        github_context = {
            "available": True,
            "repositories": [
                {
                    "repo_full_name": "example/repo",
                    "project_keys": ["TP"],
                    "open_pull_requests": [{"number": 42, "title": "Update staging flow"}],
                }
            ],
        }
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_github_ask_context", return_value=github_context),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "Board answer"},
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_codex",
                return_value="Board answer",
            ),
        ):
            command_response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask review staging against current tickets",
                ),
                session=session,
                require_ask_confirmation=True,
            )

        self.assertTrue(command_response.ok)
        self.assertEqual(plan_mock.call_args.kwargs["github_context"], github_context)

    def test_ask_follow_up_reuses_recent_scoped_issue_key(self) -> None:
        collect_calls: list[str | None] = []

        def _collect_stub(*, scoped_issue_key, **_kwargs):  # type: ignore[no-untyped-def]
            collect_calls.append(scoped_issue_key)
            return (
                scoped_issue_key.strip().upper() if isinstance(scoped_issue_key, str) and scoped_issue_key.strip() else None,
                None,
                [{"key": "TP-77", "summary": "Investigate", "status": "To Do"}],
                {"To Do": 1},
                [],
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context", side_effect=_collect_stub),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_codex", return_value="Board answer"),
        ):
            first = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask @TP-77 summarize status",
                ),
                session=session,
            )
            second = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask what changed since last update?",
                ),
                session=session,
            )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(collect_calls[0], "TP-77")
        self.assertEqual(collect_calls[1], "TP-77")

    def test_ask_board_message_passes_github_context_to_codex(self) -> None:
        github_context = {
            "available": True,
            "repositories": [
                {
                    "repo_full_name": "example/repo",
                    "project_keys": ["TP"],
                    "open_pull_requests": [{"number": 7, "title": "Refactor worker"}],
                }
            ],
        }
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {}, []),
            ),
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_github_ask_context", return_value=github_context),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_codex", return_value="Board answer") as answer_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask compare staging to in-progress tickets",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(answer_mock.call_args.kwargs["github_context"], github_context)

    def test_collect_github_ask_context_partitions_staging_prs(self) -> None:
        fake_prs = [
            SimpleNamespace(
                number=1,
                title="Feature to staging",
                state="open",
                head_ref="jira/feature-1",
                base_ref="staging",
                html_url="https://github.com/example/repo/pull/1",
                updated_at="2026-02-12T17:00:00Z",
            ),
            SimpleNamespace(
                number=2,
                title="Feature to main",
                state="open",
                head_ref="jira/feature-2",
                base_ref="main",
                html_url="https://github.com/example/repo/pull/2",
                updated_at="2026-02-12T17:05:00Z",
            ),
        ]
        fake_client = SimpleNamespace(list_open_pull_requests=lambda **_kwargs: fake_prs)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with (
                patch("orchestrator.api.discord.ingress.ask_runtime.github_client_from_tenant_config", return_value=fake_client),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_local_repo_context",
                    return_value=SimpleNamespace(
                        available=True,
                        reason=None,
                        repo_dir="/tmp/repo",
                        current_branch="staging",
                        head_sha="abc123",
                        branches=["staging", "jira/TP-1"],
                        recent_commits=["abc123 TP-1: update"],
                    ),
                ),
            ):
                context = collect_github_ask_context(
                    session=session,
                    tenant=tenant,
                    project_keys=["TP"],
                )

        self.assertTrue(context["available"])
        repositories = context["repositories"]
        self.assertEqual(len(repositories), 1)
        self.assertEqual(len(repositories[0]["open_pull_requests"]), 2)
        self.assertEqual(len(repositories[0]["staging_pull_requests"]), 1)
        self.assertEqual(repositories[0]["staging_pull_requests"][0]["number"], 1)
        self.assertTrue(repositories[0]["local_repo"]["available"])
        self.assertEqual(repositories[0]["local_repo"]["current_branch"], "staging")

    def test_collect_github_ask_context_marks_degraded_when_pr_fetch_fails(self) -> None:
        fake_client = SimpleNamespace(
            list_open_pull_requests=lambda **_kwargs: (_ for _ in ()).throw(GitHubApiError("rate limited"))
        )
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with (
                patch("orchestrator.api.discord.ingress.ask_runtime.github_client_from_tenant_config", return_value=fake_client),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_local_repo_context",
                    return_value=SimpleNamespace(
                        available=False,
                        reason="repository_not_cloned",
                        repo_dir="/tmp/repo",
                        current_branch=None,
                        head_sha=None,
                        branches=[],
                        recent_commits=[],
                    ),
                ),
            ):
                context = collect_github_ask_context(
                    session=session,
                    tenant=tenant,
                    project_keys=["TP"],
                )

        self.assertFalse(context["available"])
        self.assertEqual(context["reason"], "github_pull_requests_unavailable")
        self.assertIn("example/repo", context["degraded_repositories"])

    def test_collect_github_ask_context_uses_platform_for_unscoped_refs(self) -> None:
        fake_client = SimpleNamespace(
            list_open_pull_requests=lambda **_kwargs: []
        )

        def _scoped_secret_lookup(
            session,
            secret_ref: str,
            encryption_key: str,
            tenant_id: str,
            project_id: str | None = None,
        ) -> str | None:
            self.assertEqual(encryption_key, get_settings().secrets_encryption_key)
            self.assertEqual(tenant_id, self.tenant_id)
            if secret_ref == f"tenant/{self.tenant_id}/GITHUB_APP_ID":
                return "tenant-app-id"
            if secret_ref == f"tenant/{self.tenant_id}/GITHUB_APP_PRIVATE_KEY":
                return "tenant-private-key"
            return None

        def _platform_secret_lookup(session, *, secret_ref: str, encryption_key: str, allow_environment_fallback: bool = True) -> str | None:
            self.assertEqual(encryption_key, get_settings().secrets_encryption_key)
            if secret_ref == "GITHUB_APP_ID":
                return "platform-app-id"
            if secret_ref == "GITHUB_APP_PRIVATE_KEY":
                return "platform-private-key"
            return None

        def _github_client_factory(
            config: dict,
            *,
            tenant_secret_lookup=None,
            platform_secret_lookup=None,
            **_: object,
        ) -> SimpleNamespace:
            self.assertIsNotNone(tenant_secret_lookup)
            self.assertIsNotNone(platform_secret_lookup)
            self.assertEqual(
                tenant_secret_lookup(f"tenant/{self.tenant_id}/GITHUB_APP_ID"),
                "tenant-app-id",
            )
            self.assertEqual(
                tenant_secret_lookup(f"tenant/{self.tenant_id}/GITHUB_APP_PRIVATE_KEY"),
                "tenant-private-key",
            )
            self.assertEqual(platform_secret_lookup("GITHUB_APP_ID"), "platform-app-id")
            self.assertEqual(platform_secret_lookup("GITHUB_APP_PRIVATE_KEY"), "platform-private-key")
            return fake_client

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with (
                patch("orchestrator.api.discord.ingress.ask_runtime.resolve_scoped_secret_ref", side_effect=_scoped_secret_lookup),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.resolve_platform_secret_ref",
                    side_effect=_platform_secret_lookup,
                ),
                patch("orchestrator.api.discord.ingress.ask_runtime.github_client_from_tenant_config", side_effect=_github_client_factory),
                patch(
                    "orchestrator.api.discord.ingress.ask_runtime.collect_local_repo_context",
                    return_value=SimpleNamespace(
                        available=True,
                        reason=None,
                        repo_dir="/tmp/repo",
                        current_branch="staging",
                        head_sha="abc123",
                        branches=["staging", "jira/TP-1"],
                        recent_commits=[],
                    ),
                ),
            ):
                context = collect_github_ask_context(
                    session=session,
                    tenant=tenant,
                    project_keys=["TP"],
                )

        self.assertTrue(context["available"])
        self.assertEqual(len(context["repositories"]), 1)
        self.assertEqual(context["repositories"][0]["repo_full_name"], "example/repo")

    def test_ask_history_scope_isolated_by_channel(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
                "discord": {"channel_id": "discord-channel-2", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)

        collect_calls: list[tuple[str | None, str | None]] = []

        def _collect_stub(*, channel_id, scoped_issue_key, **_kwargs):  # type: ignore[no-untyped-def]
            collect_calls.append((channel_id, scoped_issue_key))
            return (
                scoped_issue_key.strip().upper() if isinstance(scoped_issue_key, str) and scoped_issue_key.strip() else None,
                None,
                [{"key": "TP-77", "summary": "Investigate", "status": "To Do"}],
                {"To Do": 1},
                [],
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context", side_effect=_collect_stub),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_codex", return_value="Board answer"),
        ):
            first = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask @TP-77 summarize status",
                ),
                session=session,
            )
            second = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-2",
                    command="!ask what changed since last update?",
                ),
                session=session,
            )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(collect_calls[0], ("discord-channel-1", "TP-77"))
        self.assertEqual(collect_calls[1], ("discord-channel-2", None))

    def test_status_and_runs_are_scoped_to_project_channel(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
                "discord": {"channel_id": "discord-channel-2", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        self._queue_run(run_id="run-default-queued", issue_key="TP-10", status="queued")
        self._queue_run(run_id="run-default-running", issue_key="TP-11", status="running")
        self._queue_run(run_id="run-other-queued", issue_key="OTH-10", status="queued", project_id=other_project_id)
        self._queue_run(run_id="run-other-running", issue_key="OTH-11", status="running", project_id=other_project_id)

        status_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-2", "command": "!status"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertNotIn("webhook_last_seen", status_response.json()["data"])
        self.assertEqual(status_response.json()["data"]["queue_depth"], 1)
        self.assertEqual(len(status_response.json()["data"]["active_runs"]), 1)
        self.assertEqual(status_response.json()["data"]["active_runs"][0]["issue_key"], "OTH-11")

        runs_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-2", "command": "!runs 10"},
        )
        self.assertEqual(runs_response.status_code, 200)
        run_issue_keys = [entry["issue_key"] for entry in runs_response.json()["data"]["runs"]]
        self.assertIn("OTH-10", run_issue_keys)
        self.assertIn("OTH-11", run_issue_keys)
        self.assertNotIn("TP-10", run_issue_keys)
        self.assertNotIn("TP-11", run_issue_keys)

    def test_status_and_runs_with_dm_channel_use_tenant_scope(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
                "discord": {"channel_id": "discord-channel-2", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)
        other_project_id = create_project.json()["project_id"]

        self._queue_run(run_id="run-default-queued", issue_key="TP-10", status="queued")
        self._queue_run(run_id="run-other-running", issue_key="OTH-11", status="running", project_id=other_project_id)

        status_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": None, "command": "!status"},
        )
        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["data"]["queue_depth"], 1)
        self.assertEqual(len(status_response.json()["data"]["active_runs"]), 1)
        self.assertEqual(status_response.json()["data"]["active_runs"][0]["issue_key"], "OTH-11")

        runs_response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": None, "command": "!runs 10"},
        )
        self.assertEqual(runs_response.status_code, 200)
        run_issue_keys = {entry["issue_key"] for entry in runs_response.json()["data"]["runs"]}
        self.assertIn("TP-10", run_issue_keys)
        self.assertIn("OTH-11", run_issue_keys)

    def test_ask_command_surfaces_jira_provider_outage(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
            side_effect=HTTPException(status_code=502, detail="Failed to query Jira board: Bad Gateway"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask what is blocked"},
            )
        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to query Jira board", response.json()["detail"])

    def test_ask_command_with_dm_channel_uses_tenant_scope(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="TP-50", summary="DM issue", status="To Do")],
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_codex", return_value="DM scoped answer"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": None, "command": "!ask what is on the board"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "DM scoped answer")

    def test_ask_ingress_scope_contract_matrix(self) -> None:
        create_project = self.client.post(
            f"/api/admin/tenants/{self.tenant_id}/projects",
            json={
                "name": "Other Project",
                "github_repository": "https://github.com/example/other",
                "jira_project_key": "OTH",
                "discord": {"channel_id": "discord-channel-2", "notify_events": []},
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201)

        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="OTH-50", summary="Scoped issue", status="To Do")],
            ) as search_mock,
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_codex", return_value="Scoped answer"),
        ):
            mapped = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": "discord-channel-2", "command": "!ask scoped"},
            )
            self.assertEqual(mapped.status_code, 200)
            mapped_jql = str(search_mock.call_args.kwargs["jql"])
            self.assertIn('project = "OTH"', mapped_jql)

            dm = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": None, "command": "!ask unscoped"},
            )
            self.assertEqual(dm.status_code, 409)
            self.assertIn("requires a single mapped project scope", dm.json()["detail"])

            with self.session_factory() as session:
                with self.assertRaises(HTTPException) as jira_ctx:
                    execute_discord_command(
                        tenant_id=self.tenant_id,
                        payload=DiscordCommandRequest(
                            user_id="jira-user-1",
                            channel_id=None,
                            command="!ask via-jira",
                        ),
                        session=session,
                        ingress_source="jira_comment",
                    )
            self.assertEqual(jira_ctx.exception.status_code, 409)
            self.assertIn("requires a single mapped project scope", str(jira_ctx.exception.detail))

        unmapped = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-unmapped", "command": "!ask blocked"},
        )
        self.assertEqual(unmapped.status_code, 403)
        self.assertIn("does not match", unmapped.json()["detail"])

    def test_ask_follow_up_drops_deleted_history_issue_key(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            discord_config = dict(tenant.discord_config or {})
            discord_config["ask_history"] = [
                {
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "question": "What changed?",
                    "answer": "Previous answer",
                    "issue_key": "TP-404",
                    "status": None,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            tenant.discord_config = discord_config
            jira_config = dict(tenant.jira_config or {})
            jira_config["connection_id"] = "connection-for-prune-test"
            tenant.jira_config = jira_config
            session.commit()

        collect_calls: list[str | None] = []

        def _collect_stub(*, scoped_issue_key, **_kwargs):  # type: ignore[no-untyped-def]
            collect_calls.append(scoped_issue_key)
            return (
                scoped_issue_key.strip().upper() if isinstance(scoped_issue_key, str) and scoped_issue_key.strip() else None,
                None,
                [{"key": "TP-77", "summary": "Investigate", "status": "To Do"}],
                {"To Do": 1},
                [],
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.ask_history_runtime.existing_issue_keys_for_tenant", return_value=set()),
            patch("orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context", side_effect=_collect_stub),
            patch("orchestrator.api.discord.commands.ask.build_codex_runtime"),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_codex", return_value="Board answer"),
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="!ask what changed since last update?",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.message, "Board answer")
        self.assertEqual(collect_calls, [None])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            history_entries = [
                entry
                for entry in (tenant.discord_config or {}).get("ask_history", [])
                if entry.get("user_id") == "u-viewer" and entry.get("channel_id") == "discord-channel-1"
            ]
        self.assertFalse(
            any(str(entry.get("issue_key") or "").strip().upper() == "TP-404" for entry in history_entries)
        )

    def test_plain_text_is_treated_as_implicit_ask_when_enabled(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [], {"Blocked": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_codex",
                return_value={"mode": "answer", "summary": "answer"},
            ),
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_codex",
                return_value="Implicit ask answer",
            ),
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="what is blocked on this board?",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "ask")
        self.assertEqual(response.message, "Implicit ask answer")

    def test_gap_command_requires_issue_key(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!gap"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !gap", response.json()["detail"])

    def test_gap_command_returns_analysis(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.gap_runtime.run_gap_analysis",
            return_value=(
                "Gap analysis for [TP-77](https://example.atlassian.net/browse/TP-77)",
                {
                    "issue_key": "TP-77",
                    "jira_url": "https://example.atlassian.net/browse/TP-77",
                    "pr_url": "https://github.com/example/repo/pull/12",
                    "confidence": "medium",
                },
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!gap TP-77",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "gap")
        self.assertEqual(response.json()["data"]["issue_key"], "TP-77")
        self.assertIn("TP-77", response.json()["message"])

    def test_plain_text_in_seed_followup_thread_routes_to_issues_followup(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            discord_config = dict(tenant.discord_config or {})
            discord_config["allowed_user_ids"] = ["u-viewer"]
            tenant.discord_config = discord_config
            store_seed_followup_context(
                session=session,
                tenant=tenant,
                request_id="followup-1",
                user_id="u-viewer",
                channel_ids=["discord-channel-1"],
                project_id=self.default_project_id,
                project_key="TP",
                issue_keys=["TP-11"],
                questions=["What is the rollout plan?"],
                prompt_markdown="Original seed prompt",
            )
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_codex",
                return_value=(
                    "PM parent issue upsert complete. Updated 1: TP-11. Created 0: none.",
                    {
                        "requires_input": False,
                        "project_key": "TP",
                        "questions": [],
                        "all_parent_issue_keys": ["TP-11"],
                    },
                ),
            ) as seed_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="Here are the missing rollout details",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "issues")
        self.assertIn("PM parent issue upsert complete", response.message)
        seed_mock.assert_called_once()
        kwargs = seed_mock.call_args.kwargs
        self.assertEqual(kwargs["allow_create"], False)
        self.assertEqual(kwargs["force_issue_keys"], ["TP-11"])
        self.assertIn("Here are the missing rollout details", kwargs["prompt_markdown"])

    def test_plain_text_in_seed_followup_thread_beats_plain_ask_routing(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            store_seed_followup_context(
                session=session,
                tenant=tenant,
                request_id="followup-plain-1",
                user_id="u-viewer",
                channel_ids=["discord-channel-1"],
                project_id=self.default_project_id,
                project_key="TP",
                issue_keys=["TP-11"],
                questions=["What is the rollout plan?"],
                prompt_markdown="Original seed prompt",
            )
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_codex",
                return_value=(
                    "PM parent issue upsert complete. Updated 1: TP-11. Created 0: none.",
                    {
                        "requires_input": False,
                        "project_key": "TP",
                        "questions": [],
                        "all_parent_issue_keys": ["TP-11"],
                    },
                ),
            ) as seed_mock,
        ):
            response = execute_discord_command(
                tenant_id=self.tenant_id,
                payload=DiscordCommandRequest(
                    user_id="u-viewer",
                    channel_id="discord-channel-1",
                    command="More rollout details",
                ),
                session=session,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "issues")
        seed_mock.assert_called_once()

    def test_execute_discord_command_rejects_unknown_ingress_source(self) -> None:
        with self.session_factory() as session:
            with self.assertRaises(HTTPException) as exc:
                execute_discord_command(
                    tenant_id=self.tenant_id,
                    payload=DiscordCommandRequest(
                        user_id="u-admin",
                        channel_id="discord-channel-1",
                        command="!help",
                    ),
                    session=session,
                    ingress_source="slack",  # type: ignore[arg-type]
                )
        self.assertEqual(exc.exception.status_code, 400)
        self.assertIn("Unsupported ingress source", str(exc.exception.detail))

    def test_issues_seed_requires_spec(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!issues seed"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !issues seed", response.json()["detail"])

    def test_bug_command_requires_summary(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!bug"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("Usage: !bug", response.json()["detail"])

    def test_bug_command_creates_jira_bug_from_params_and_attachments(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.bug_runtime.create_discord_bug_issue",
            return_value=(
                "Bug logged: [TP-501](https://example.atlassian.net/browse/TP-501)",
                {"created_issue_keys": ["TP-501"]},
            ),
        ) as create_bug_mock:
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-viewer",
                    "channel_id": "discord-channel-1",
                    "command": "!bug",
                    "command_params": {
                        "summary": "Login fails on mobile",
                        "details": "Tap login, spinner loops forever.",
                        "issue_key": "TP-77",
                    },
                    "attachments": [
                        {
                            "id": "a1",
                            "filename": "screenshot.png",
                            "url": "https://cdn.discordapp.com/attachments/1.png",
                            "content_type": "image/png",
                        }
                    ],
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "bug")
        self.assertIn("TP-501", response.json()["message"])
        create_bug_mock.assert_called_once()
        kwargs = create_bug_mock.call_args.kwargs
        self.assertEqual(kwargs["summary"], "Login fails on mobile")
        self.assertEqual(kwargs["related_issue_key"], "TP-77")
        self.assertEqual(kwargs["attachments"][0]["filename"], "screenshot.png")

    def test_issues_seed_calls_codex_seed_flow(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.seed_runtime.seed_parent_issues_with_codex",
            return_value=(
                "PM parent issue upsert complete. Created 2: TP-1, TP-2. Updated 0: none.",
                {"created_parent_issue_keys": ["TP-1", "TP-2"]},
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!issues seed Build API and webhook tasks",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["command"], "issues")
        self.assertIn("TP-1", response.json()["message"])

    def test_issues_seed_with_incomplete_oauth_context_returns_controlled_502(self) -> None:
        with (
            patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=object()),
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.plan_pm_parent_issues_with_codex",
                return_value={
                    "project_key": "TP",
                    "issues": [
                        {
                            "summary": "Build API and webhook reliability feature",
                            "issue_type": "Story",
                            "objective": "Improve reliability",
                            "user_value": "Customers see fewer delivery failures",
                            "recommendation": "Ship API validation plus webhook retries",
                            "scope_in": ["API changes"],
                            "scope_out": [],
                            "acceptance_criteria": ["Validation passes"],
                            "ui_references": [],
                            "risks": [],
                            "open_questions": [],
                            "success_outcomes": ["Lower webhook failure rate"],
                            "labels": [],
                        }
                    ],
                },
            ),
            patch(
                "orchestrator.api.discord.ingress.jira_runtime.tenant_jira_oauth_context",
                return_value={"access_token": "tok-only"},
            ),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={
                    "user_id": "u-admin",
                    "channel_id": "discord-channel-1",
                    "command": "!issues seed Build API and webhook tasks",
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("Failed to seed Jira issues", response.json()["detail"])
        self.assertIn("Jira OAuth context is incomplete", response.json()["detail"])
        self.assertNotIn("tok-only", response.json()["detail"])
        self.assertNotIn("Internal server error. Ref:", response.json()["detail"])

    def test_seed_issues_requests_clarifications_when_required_fields_missing(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-seed-clarify"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-seed-clarify",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def search_issues_by_jql(self, **_: object) -> list[JiraIssuePreview]:  # noqa: ANN003
                return []

            def create_issue(self, **kwargs: object) -> JiraIssueCreateResult:  # noqa: ANN003
                issue = kwargs["issue"]
                if getattr(issue, "parent_issue_key", None):
                    return JiraIssueCreateResult(key="TP-301", issue_id="301")
                return JiraIssueCreateResult(key="TP-300", issue_id="300")

            def update_issue_fields(self, **_: object) -> None:  # noqa: ANN003
                return None

            def add_issue_link(self, **_: object) -> dict:  # noqa: ANN003
                return {}

        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=object()),
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.plan_seed_issues_with_codex",
                return_value={
                    "project_key": "TP",
                    "parent_issue": {
                        "summary": "Improve worker retry reliability",
                        "issue_type": "Story",
                        "objective": "Improve reliability",
                        "user_value": "Operators see fewer worker failures",
                        "recommendation": "Ship bounded retries first",
                        "scope_in": ["Worker retry strategy"],
                        "scope_out": ["UI changes"],
                        "acceptance_criteria": ["Retries are bounded and observable"],
                        "ui_references": [],
                        "dependencies": [],
                        "risks": [],
                        "open_questions": [],
                        "success_outcomes": ["Lower worker retry failures"],
                        "labels": ["seeded"],
                    },
                    "questions": ["What is the rollout plan?"],
                    "engineering_children": [
                        {
                            "summary": "Create worker retries",
                            "issue_type": "Sub-task",
                            "behavior_slice": "Retry failed worker jobs safely.",
                            "technical_objective": "Add bounded worker retries.",
                            "implementation_plan": [],
                            "technical_dependencies": [],
                            "risks": [],
                            "how_to_test": [],
                            "done_criteria": [],
                            "labels": ["seeded"],
                        }
                    ],
                },
            ),
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=_FakeClient()),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = seed_issues_with_codex(
                session=session,
                tenant=tenant,
                prompt_markdown="Seed issues from spec",
                scoped_project_id=self.default_project_id,
            )

        self.assertIn("need more detail", message.lower())
        self.assertTrue(data["requires_input"])
        self.assertEqual(data["created_issue_keys"], ["TP-300", "TP-301"])
        self.assertIn("What is the rollout plan?", data["questions"])
        self.assertEqual(data["questions"], ["What is the rollout plan?"])
        self.assertEqual(data["children_sync_status"], "sync_blocked")

    def test_seed_issues_updates_matching_existing_issue(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-seed-upsert"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-seed-upsert",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def __init__(self) -> None:
                self.updated_issue_keys: list[str] = []
                self.create_called = False

            def search_issues_by_jql(self, **_: object) -> list[JiraIssuePreview]:  # noqa: ANN003
                return [
                    JiraIssuePreview(key="TP-110", summary="Improve worker retry reliability", status="To Do"),
                    JiraIssuePreview(key="TP-111", summary="Create worker retries", status="To Do"),
                ]

            def update_issue_fields(self, **kwargs: object) -> None:  # noqa: ANN003
                self.updated_issue_keys.append(str(kwargs["issue_id_or_key"]))

            def create_issue(self, **_: object) -> JiraIssueCreateResult:  # noqa: ANN003
                self.create_called = True
                return JiraIssueCreateResult(key="TP-999", issue_id="999")

            def add_issue_link(self, **_: object) -> dict:  # noqa: ANN003
                return {}

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.seed_runtime.build_codex_runtime", return_value=object()),
            patch(
                "orchestrator.api.discord.ingress.seed_runtime.plan_seed_issues_with_codex",
                return_value={
                    "project_key": "TP",
                    "parent_issue": {
                        "summary": "Improve worker retry reliability",
                        "issue_type": "Story",
                        "objective": "Improve reliability",
                        "user_value": "Operators see fewer worker failures",
                        "recommendation": "Ship bounded retries first",
                        "scope_in": ["Worker retry strategy"],
                        "scope_out": ["UI changes"],
                        "acceptance_criteria": ["Retries are bounded and observable"],
                        "ui_references": [],
                        "dependencies": [],
                        "risks": [],
                        "open_questions": [],
                        "success_outcomes": ["Lower worker retry failures"],
                        "labels": ["seeded"],
                    },
                    "engineering_children": [
                        {
                            "summary": "Create worker retries",
                            "issue_type": "Sub-task",
                            "behavior_slice": "Retry failed worker jobs safely.",
                            "technical_objective": "Add bounded worker retries.",
                            "implementation_plan": ["Worker retry strategy"],
                            "technical_dependencies": [],
                            "risks": [],
                            "how_to_test": [],
                            "done_criteria": ["Retries are bounded and observable"],
                            "labels": ["seeded"],
                        }
                    ],
                },
            ),
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = seed_issues_with_codex(
                session=session,
                tenant=tenant,
                prompt_markdown="Seed issues from spec",
                scoped_project_id=self.default_project_id,
            )

        self.assertIn("Updated 2", message)
        self.assertEqual(data["updated_issue_keys"], ["TP-110", "TP-111"])
        self.assertEqual(data["created_issue_keys"], [])
        self.assertEqual(fake_client.updated_issue_keys, ["TP-110", "TP-111", "TP-110"])
        self.assertFalse(fake_client.create_called)

    def test_seed_issue_description_is_native_jira_adf(self) -> None:
        description = build_seed_issue_description(
            objective="Ship feature",
            scope_in=["API endpoint"],
            scope_out=["Mobile app changes"],
            acceptance_criteria=["Endpoint returns 200"],
            how_to_test=["Run API integration tests"],
            nfr_intent="MVP",
            dependencies_and_risks=["Depends on staging API availability"],
        )
        self.assertEqual(description.get("type"), "doc")
        content = description.get("content", [])
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "heading")
        self.assertEqual(content[0]["content"][0]["text"], "Technical Objective")
        self.assertEqual(content[1]["type"], "bulletList")
        first_bullet = content[1]["content"][0]["content"][0]["content"][0]["text"]
        self.assertEqual(first_bullet, "Ship feature")
        heading_texts = [node["content"][0]["text"] for node in content if node.get("type") == "heading"]
        self.assertIn("How to Test", heading_texts)
        self.assertIn("Synced From Parent Revision", heading_texts)

    def test_bug_creation_uploads_discord_attachments_to_jira_issue(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-1"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-1",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def __init__(self) -> None:
                self.upload_calls: list[dict] = []
                self.created_issues: list[object] = []

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-901", issue_id="901")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                self.upload_calls.append(dict(kwargs))
                return [{"id": "att-1"}]

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value="triage-bugs"),
            patch(
                "orchestrator.api.discord.ingress.bug_runtime.download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Login fails",
                details="See screenshot",
                reporter_user_id="u-viewer",
                channel_id="discord-channel-1",
                related_issue_key=None,
                attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
                selected_project_key="TP",
            )

        self.assertIn("Attached 1/1 file(s)", message)
        self.assertEqual(len(fake_client.upload_calls), 1)
        self.assertEqual(fake_client.upload_calls[0]["issue_id_or_key"], "TP-901")
        self.assertEqual(len(fake_client.created_issues), 1)
        created_description = str(fake_client.created_issues[0].description)
        self.assertIn("Channel: triage-bugs (discord-channel-1)", created_description)
        self.assertIn("screen.png", created_description)
        self.assertNotIn("https://cdn.discordapp.com/x.png", created_description)

    def test_bug_creation_fails_hard_on_attachment_failure(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-attach-fail"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-attach-fail",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def __init__(self) -> None:
                self.created_issues: list[object] = []

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-903", issue_id="903")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                del kwargs
                raise JiraOAuthError("Jira attachment upload failed (403): permission denied")

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value="triage-bugs"),
            patch(
                "orchestrator.api.discord.ingress.bug_runtime.download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with self.assertRaises(HTTPException) as exc:
                create_discord_bug_issue(
                    session=session,
                    tenant=tenant,
                    summary="Login fails",
                    details="See screenshot",
                    reporter_user_id="u-viewer",
                    channel_id="discord-channel-1",
                    related_issue_key=None,
                    attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
                    selected_project_key="TP",
                )
        self.assertEqual(exc.exception.status_code, 502)
        self.assertIn("Bug created as TP-903", str(exc.exception.detail))
        self.assertIn("permission denied", str(exc.exception.detail))
        self.assertIn("Jira upload", str(exc.exception.detail))

    def test_bug_creation_falls_back_to_channel_id_when_name_lookup_unavailable(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-2"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-2",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def __init__(self) -> None:
                self.created_issues: list[object] = []

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-902", issue_id="902")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                return [{"id": "att-1"}]

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value=None),
            patch(
                "orchestrator.api.discord.ingress.bug_runtime.download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Login fails",
                details="See screenshot",
                reporter_user_id="u-viewer",
                channel_id="discord-channel-1",
                related_issue_key=None,
                attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
                selected_project_key="TP",
            )
        description = str(fake_client.created_issues[0].description)
        self.assertIn("Channel: discord-channel-1", description)

    def test_bug_creation_uses_selected_scoped_project_key(self) -> None:
        now = datetime.now(timezone.utc)
        self._create_project(project_id=f"{self.tenant_id}-other", jira_project_key="OTH", channel_id="discord-other-1")
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-3"
            tenant.jira_config = jira_config
            session.add(
                JiraOAuthConnection(
                    connection_id="conn-3",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted="enc",
                    refresh_token_encrypted="enc",
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        class _FakeClient:
            def __init__(self) -> None:
                self.project_key: str | None = None

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.project_key = str(kwargs.get("project_key"))
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="OTH-902", issue_id="902")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:  # noqa: ANN003
                return []

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value="other"),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Scoped bug",
                details="Details",
                reporter_user_id="u-viewer",
                channel_id="discord-other-1",
                related_issue_key=None,
                attachments=[],
                selected_project_key="OTH",
            )

        self.assertEqual(fake_client.project_key, "OTH")

    def test_bug_creation_requires_scoped_project_key(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            with self.assertRaises(HTTPException) as exc:
                create_discord_bug_issue(
                    session=session,
                    tenant=tenant,
                    summary="Missing scope",
                    details="Details",
                    reporter_user_id="u-viewer",
                    channel_id="discord-channel-1",
                    related_issue_key=None,
                    attachments=[],
                    selected_project_key=None,
                )
        self.assertEqual(exc.exception.status_code, 409)
        self.assertIn("project-scoped", str(exc.exception.detail))

    def test_build_discord_bug_description_lists_attachments_without_hyperlinks(self) -> None:
        description = build_discord_bug_description(
            summary="Login fails",
            details="Details",
            reporter_user_id="u-viewer",
            channel_id="triage-bugs (discord-channel-1)",
            related_issue_key="TP-77",
            attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
        )
        self.assertIn("screen.png", description)
        self.assertNotIn("https://cdn.discordapp.com/x.png", description)
        self.assertIn("Channel: triage-bugs (discord-channel-1)", description)
        self.assertIn("Reported via Discord", description)
        self.assertIn("Summary", description)

    def test_request_creates_pending_request(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-viewer",
                "channel_id": "discord-channel-1",
                "command": "!request run_controls Need run controls",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("Allowlist request", response.json()["message"])

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            project = session.get(Project, f"{self.tenant_id}-default")
            self.assertIsNotNone(project)
            requests = (project.discord_config or {}).get("allowlist_requests", [])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["user_id"], "u-viewer")
            self.assertEqual(requests[0]["permissions"], ["run_controls"])
            self.assertEqual(requests[0]["reason"], "Need run controls")

    def test_request_for_allowlisted_user_returns_already_allowlisted(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-admin",
                "channel_id": "discord-channel-1",
                "command": "!request run_controls Please add me",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertIn("already allowlisted", response.json()["message"])
