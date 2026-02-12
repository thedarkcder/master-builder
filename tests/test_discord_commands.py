import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from fastapi import HTTPException

from orchestrator.api.main import create_app
from orchestrator.api.routes.discord import (
    _ask_board_message,
    _build_discord_bug_description,
    _build_seed_issue_description,
    _collect_github_ask_context,
    _collect_ask_context,
    _create_discord_bug_issue,
    _project_filter_jql,
    _seed_issues_with_codex,
    execute_discord_command,
)
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import JiraOAuthConnection, Project, Run, Tenant
from orchestrator.tools.jira_oauth import JiraIssueBulkCreateResult, JiraIssueCreateResult, JiraIssuePreview


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

    def tearDown(self) -> None:
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
            "orchestrator.api.routes.discord._fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!run TP-20"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-20")

    def test_run_conflict_includes_active_run_details(self) -> None:
        self._queue_run(run_id="run-active-1", issue_key="TP-20", status="running")
        with patch(
            "orchestrator.api.routes.discord._fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-20", summary="Do thing", status="To Do"),
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
            "orchestrator.api.routes.discord._fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        self.assertEqual(response.json()["data"]["issue_key"], "TP-30")

    def test_retry_conflict_includes_active_run_details(self) -> None:
        self._queue_run(run_id="run-failed-1", issue_key="TP-30", status="failed")
        self._queue_run(run_id="run-active-2", issue_key="TP-30", status="queued")
        with patch(
            "orchestrator.api.routes.discord._fetch_jira_issue_preview",
            return_value=JiraIssuePreview(key="TP-30", summary="Retry thing", status="To Do"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-admin", "channel_id": "discord-channel-1", "command": "!retry run-failed-1"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertIn("run_already_active", response.json()["detail"])
        self.assertIn("run-active-2", response.json()["detail"])

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
            "orchestrator.api.routes.discord._fetch_jira_issue_preview",
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
            "orchestrator.api.routes.discord._fetch_jira_issue_preview",
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

    def test_ask_command_returns_board_answer(self) -> None:
        with patch(
            "orchestrator.api.routes.discord._ask_board_message",
            return_value=("Board snapshot", {"status_counts": {"To Do": 2}}),
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
        with patch(
            "orchestrator.api.routes.discord._ask_board_message",
            return_value=("Issue snapshot", {"issue_key": "TP-101"}),
        ) as ask_mock:
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
        self.assertEqual(ask_mock.call_args.kwargs["scoped_issue_key"], "TP-101")

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
                _project_filter_jql(session=session, tenant=tenant, channel_id="discord-channel-1"),
                'project = "TP"',
            )
            self.assertEqual(
                _project_filter_jql(session=session, tenant=tenant, channel_id="discord-channel-2"),
                'project = "OTH"',
            )
            with self.assertRaises(HTTPException):
                _project_filter_jql(session=session, tenant=tenant, channel_id="discord-unmapped")
            self.assertIn(
                _project_filter_jql(session=session, tenant=tenant, channel_id="__dm__"),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )
            self.assertIn(
                _project_filter_jql(session=session, tenant=tenant, channel_id="dm"),
                {'project in ("TP", "OTH")', 'project in ("OTH", "TP")'},
            )
            self.assertIn(
                _project_filter_jql(session=session, tenant=tenant),
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

            jql = _project_filter_jql(session=session, tenant=tenant, channel_id="dm")
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

            with patch("orchestrator.api.routes.discord._search_jira_issues_for_tenant", return_value=[]) as search_mock:
                _collect_ask_context(
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
                    "orchestrator.api.routes.discord._collect_ask_context_with_history_context",
                    return_value=(
                        None,
                        None,
                        [{"key": "OTH-1", "summary": "Other item", "status": "To Do"}],
                        {"To Do": 1},
                        [],
                    ),
                ),
                patch("orchestrator.api.routes.discord.build_codex_runtime"),
                patch("orchestrator.api.routes.discord.answer_board_question_with_codex", return_value="Board answer") as answer_mock,
            ):
                message, _ = _ask_board_message(
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
                _project_filter_jql(session=session, tenant=tenant, channel_id="discord-project-thread-1"),
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
                "orchestrator.api.routes.discord._collect_ask_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}),
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
                "orchestrator.api.routes.discord._collect_ask_context",
                return_value=(None, None, [{"key": "OTH-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}),
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
                "orchestrator.api.routes.discord._collect_ask_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}),
            ),
            patch("orchestrator.api.routes.discord._collect_github_ask_context", return_value=github_context),
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
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.routes.discord._collect_ask_context", side_effect=_collect_stub),
            patch("orchestrator.api.routes.discord.build_codex_runtime"),
            patch("orchestrator.api.routes.discord.answer_board_question_with_codex", return_value="Board answer"),
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
            patch("orchestrator.api.routes.discord._collect_ask_context", return_value=(None, None, [], {})),
            patch("orchestrator.api.routes.discord._collect_github_ask_context", return_value=github_context),
            patch("orchestrator.api.routes.discord.build_codex_runtime"),
            patch("orchestrator.api.routes.discord.answer_board_question_with_codex", return_value="Board answer") as answer_mock,
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
            with patch("orchestrator.api.routes.discord.github_client_from_tenant_config", return_value=fake_client):
                context = _collect_github_ask_context(
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
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.routes.discord._collect_ask_context", side_effect=_collect_stub),
            patch("orchestrator.api.routes.discord.build_codex_runtime"),
            patch("orchestrator.api.routes.discord.answer_board_question_with_codex", return_value="Board answer"),
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
            "orchestrator.api.routes.discord._search_jira_issues_for_tenant",
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
                "orchestrator.api.routes.discord._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="TP-50", summary="DM issue", status="To Do")],
            ),
            patch("orchestrator.api.routes.discord.build_codex_runtime"),
            patch("orchestrator.api.routes.discord.answer_board_question_with_codex", return_value="DM scoped answer"),
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
                "orchestrator.api.routes.discord._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="OTH-50", summary="Scoped issue", status="To Do")],
            ) as search_mock,
            patch("orchestrator.api.routes.discord.build_codex_runtime"),
            patch("orchestrator.api.routes.discord.answer_board_question_with_codex", return_value="Scoped answer"),
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
            self.assertEqual(dm.status_code, 200)
            dm_jql = str(search_mock.call_args.kwargs["jql"])
            self.assertIn("project in", dm_jql)
            self.assertIn('"TP"', dm_jql)
            self.assertIn('"OTH"', dm_jql)

            with self.session_factory() as session:
                jira_ingress = execute_discord_command(
                    tenant_id=self.tenant_id,
                    payload=DiscordCommandRequest(
                        user_id="jira-user-1",
                        channel_id=None,
                        command="!ask via-jira",
                    ),
                    session=session,
                    ingress_source="jira_comment",
                )
            self.assertTrue(jira_ingress.ok)
            jira_jql = str(search_mock.call_args.kwargs["jql"])
            self.assertIn("project in", jira_jql)
            self.assertIn('"TP"', jira_jql)
            self.assertIn('"OTH"', jira_jql)

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
            )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.routes.discord._existing_issue_keys_for_tenant", return_value=set()),
            patch("orchestrator.api.routes.discord._collect_ask_context", side_effect=_collect_stub),
            patch("orchestrator.api.routes.discord.build_codex_runtime"),
            patch("orchestrator.api.routes.discord.answer_board_question_with_codex", return_value="Board answer"),
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
                "orchestrator.api.routes.discord._ask_board_message",
                return_value=("Implicit ask answer", {"status_counts": {"Blocked": 1}}),
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
                allow_plain_ask=True,
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
            "orchestrator.api.routes.discord._run_gap_analysis",
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
            discord_config["seed_followups"] = [
                {
                    "request_id": "followup-1",
                    "user_id": "u-viewer",
                    "channel_ids": ["discord-channel-1"],
                    "project_key": "TP",
                    "issue_keys": ["TP-11"],
                    "questions": ["What is the rollout plan?"],
                    "prompt_markdown": "Original seed prompt",
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
            ]
            discord_config["allowed_user_ids"] = ["u-viewer"]
            tenant.discord_config = discord_config
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.routes.discord._seed_issues_with_codex",
                return_value=(
                    "Issue upsert complete. Updated 1: TP-11. Created 0: none.",
                    {
                        "requires_input": False,
                        "project_key": "TP",
                        "questions": [],
                        "all_issue_keys": ["TP-11"],
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
                allow_plain_ask=True,
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.command, "issues")
        self.assertIn("Issue upsert complete", response.message)
        seed_mock.assert_called_once()
        kwargs = seed_mock.call_args.kwargs
        self.assertEqual(kwargs["allow_create"], False)
        self.assertEqual(kwargs["force_issue_keys"], ["TP-11"])
        self.assertIn("Here are the missing rollout details", kwargs["prompt_markdown"])

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
            "orchestrator.api.routes.discord._create_discord_bug_issue",
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
            "orchestrator.api.routes.discord._seed_issues_with_codex",
            return_value=("Seeded 2 issue(s): TP-1, TP-2", {"created_issue_keys": ["TP-1", "TP-2"]}),
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

            def create_issues_bulk(self, **_: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-301", issue_id="301")],
                    errors=[],
                )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.routes.discord.build_codex_runtime", return_value=object()),
            patch(
                "orchestrator.api.routes.discord.plan_seed_issues_with_codex",
                return_value={
                    "project_key": "TP",
                    "questions": ["What is the rollout plan?"],
                    "issues": [
                        {
                            "summary": "Create worker retries",
                            "objective": "TBD",
                            "scope_in": [],
                            "scope_out": [],
                            "acceptance_criteria": [],
                            "labels": ["seeded"],
                            "issue_type": "Task",
                        }
                    ],
                },
            ),
            patch("orchestrator.api.routes.discord._refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.routes.discord._jira_oauth_client", return_value=_FakeClient()),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = _seed_issues_with_codex(
                session=session,
                tenant=tenant,
                prompt_markdown="Seed issues from spec",
            )

        self.assertIn("need more detail", message.lower())
        self.assertTrue(data["requires_input"])
        self.assertEqual(data["created_issue_keys"], ["TP-301"])
        self.assertIn("What is the rollout plan?", data["questions"])
        self.assertTrue(any("objective" in question.lower() for question in data["questions"]))

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
                return [JiraIssuePreview(key="TP-111", summary="Create worker retries", status="To Do")]

            def update_issue_fields(self, **kwargs: object) -> None:  # noqa: ANN003
                self.updated_issue_keys.append(str(kwargs["issue_id_or_key"]))

            def create_issues_bulk(self, **_: object) -> JiraIssueBulkCreateResult:  # noqa: ANN003
                self.create_called = True
                return JiraIssueBulkCreateResult(created=[], errors=[])

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.routes.discord.build_codex_runtime", return_value=object()),
            patch(
                "orchestrator.api.routes.discord.plan_seed_issues_with_codex",
                return_value={
                    "project_key": "TP",
                    "issues": [
                        {
                            "summary": "Create worker retries",
                            "objective": "Improve reliability",
                            "scope_in": ["Worker retry strategy"],
                            "scope_out": ["UI changes"],
                            "acceptance_criteria": ["Retries are bounded and observable"],
                            "labels": ["seeded"],
                            "issue_type": "Task",
                        }
                    ],
                },
            ),
            patch("orchestrator.api.routes.discord._refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.routes.discord._jira_oauth_client", return_value=fake_client),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = _seed_issues_with_codex(
                session=session,
                tenant=tenant,
                prompt_markdown="Seed issues from spec",
            )

        self.assertIn("Updated 1", message)
        self.assertEqual(data["updated_issue_keys"], ["TP-111"])
        self.assertEqual(data["created_issue_keys"], [])
        self.assertEqual(fake_client.updated_issue_keys, ["TP-111"])
        self.assertFalse(fake_client.create_called)

    def test_seed_issue_description_is_native_jira_adf(self) -> None:
        description = _build_seed_issue_description(
            objective="Ship feature",
            scope_in=["API endpoint"],
            scope_out=["Mobile app changes"],
            acceptance_criteria=["Endpoint returns 200"],
        )
        self.assertEqual(description.get("type"), "doc")
        content = description.get("content", [])
        self.assertIsInstance(content, list)
        self.assertEqual(content[0]["type"], "heading")
        self.assertEqual(content[0]["content"][0]["text"], "Objective")
        self.assertEqual(content[1]["type"], "bulletList")
        first_bullet = content[1]["content"][0]["content"][0]["content"][0]["text"]
        self.assertEqual(first_bullet, "Ship feature")

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
            patch("orchestrator.api.routes.discord._refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.routes.discord._jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.routes.discord._resolve_discord_channel_name", return_value="triage-bugs"),
            patch(
                "orchestrator.api.routes.discord._download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, data = _create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Login fails",
                details="See screenshot",
                reporter_user_id="u-viewer",
                channel_id="discord-channel-1",
                related_issue_key=None,
                attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
            )

        self.assertIn("Attached 1/1 file(s)", message)
        self.assertEqual(data["uploaded_attachment_count"], 1)
        self.assertEqual(len(fake_client.upload_calls), 1)
        self.assertEqual(fake_client.upload_calls[0]["issue_id_or_key"], "TP-901")
        self.assertEqual(len(fake_client.created_issues), 1)
        created_description = str(fake_client.created_issues[0].description)
        self.assertIn("Channel: triage-bugs (discord-channel-1)", created_description)
        self.assertIn("[screen.png](https://cdn.discordapp.com/x.png)", created_description)

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
            patch("orchestrator.api.routes.discord._refresh_jira_connection_tokens", return_value="token"),
            patch("orchestrator.api.routes.discord._jira_oauth_client", return_value=fake_client),
            patch("orchestrator.api.routes.discord._resolve_discord_channel_name", return_value=None),
            patch(
                "orchestrator.api.routes.discord._download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            _create_discord_bug_issue(
                session=session,
                tenant=tenant,
                summary="Login fails",
                details="See screenshot",
                reporter_user_id="u-viewer",
                channel_id="discord-channel-1",
                related_issue_key=None,
                attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
            )
        description = str(fake_client.created_issues[0].description)
        self.assertIn("Channel: discord-channel-1", description)

    def test_build_discord_bug_description_uses_hyperlinks_for_attachments(self) -> None:
        description = _build_discord_bug_description(
            summary="Login fails",
            details="Details",
            reporter_user_id="u-viewer",
            channel_id="triage-bugs (discord-channel-1)",
            related_issue_key="TP-77",
            attachments=[{"filename": "screen.png", "url": "https://cdn.discordapp.com/x.png"}],
        )
        self.assertIn("[screen.png](https://cdn.discordapp.com/x.png)", description)
        self.assertIn("Channel: triage-bugs (discord-channel-1)", description)

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
