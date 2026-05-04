from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from orchestrator.api.discord.ask.context import project_filter_jql
from orchestrator.api.discord.ingress.ask_runtime import ask_board_message, collect_ask_context, collect_github_ask_context
from orchestrator.api.discord.ingress.executor import execute_discord_command
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.core.config import get_settings
from orchestrator.core.runtime.payload_models import AskIntentPayload
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.github_app import GitHubApiError
from orchestrator.tools.atlassian_oauth import JiraIssuePreview
from tests.test_support.discord_command_api_harness import DiscordCommandApiTestHarness


pytestmark = pytest.mark.contract


class DiscordAskScopeAndRequestFlowTests(DiscordCommandApiTestHarness):
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
        assert create_project.status_code == 201
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
            other_project = session.get(Project, other_project_id)
            assert other_project is not None
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
        assert 'project = "OTH"' in called_jql
        assert 'project = "TP"' not in called_jql

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
        assert create_project.status_code == 201
        archived_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
            archived_project = session.get(Project, archived_project_id)
            assert archived_project is not None
            archived_project.is_archived = True
            session.commit()

            jql = project_filter_jql(session=session, tenant=tenant, channel_id="dm")
            assert jql == 'project = "TP"'
            assert "ARC" not in jql

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
        assert create_project.status_code == 201
        other_project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None

            default_project = session.get(Project, f"{self.tenant_id}-default")
            assert default_project is not None
            default_discord = dict(default_project.discord_config or {})
            default_discord["channel_id"] = "discord-channel-1"
            default_project.discord_config = default_discord

            other_project = session.get(Project, other_project_id)
            assert other_project is not None
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
                patch("orchestrator.api.discord.ingress.ask_runtime.answer_board_question_with_runtime", return_value="Board answer") as answer_mock,
            ):
                message, _ = ask_board_message(
                    session=session,
                    tenant=tenant,
                    user_id="u-viewer",
                    channel_id="discord-channel-2",
                    question="what is in progress",
                )

        assert message == "Board answer"
        assert answer_mock.call_args.kwargs["project_keys"] == ["OTH"]

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
        assert create_project.status_code == 201
        project_id = create_project.json()["project_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
            project = session.get(Project, project_id)
            assert project is not None
            discord_config = dict(project.discord_config or {})
            discord_config["ask_thread_channel_ids"] = ["discord-project-thread-1"]
            project.discord_config = discord_config
            session.commit()

            assert (
                project_filter_jql(session=session, tenant=tenant, channel_id="discord-project-thread-1")
                == 'project = "THR"'
            )

    def test_ask_command_requires_confirmation_when_intent_is_action(self) -> None:
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "TP-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(
                    mode="command",
                    summary="Queue the issue run now",
                    command="!run TP-20",
                ),
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

        assert command_response.ok is True
        assert command_response.command == "ask"
        assert isinstance(command_response.data, dict)
        assert command_response.data["requires_confirmation"] is True
        assert command_response.data["proposed_command"] == "!run TP-20"
        assert command_response.data["request_id"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
            pending = tenant.discord_config.get("pending_ask_actions", [])
            assert len(pending) == 1

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
        assert create_project.status_code == 201

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.discord.ingress.ask_runtime.collect_ask_context_with_history_context",
                return_value=(None, None, [{"key": "OTH-20", "summary": "Do thing", "status": "To Do"}], {"To Do": 1}, []),
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="Board answer", command=None),
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
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

        assert command_response.ok is True
        assert command_response.command == "ask"
        assert plan_mock.call_args.kwargs["project_keys"] == ["OTH"]

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
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="Board answer", command=None),
            ) as plan_mock,
            patch(
                "orchestrator.api.discord.commands.ask.answer_board_question_with_runtime",
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

        assert command_response.ok is True
        assert plan_mock.call_args.kwargs["github_context"] == github_context

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
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="answer", command=None),
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Board answer"),
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

        assert first.ok is True
        assert second.ok is True
        assert collect_calls[0] == "TP-77"
        assert collect_calls[1] == "TP-77"

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
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="answer", command=None),
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Board answer") as answer_mock,
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

        assert response.ok is True
        assert answer_mock.call_args.kwargs["github_context"] == github_context

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
            assert tenant is not None
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

        assert context["available"] is True
        repositories = context["repositories"]
        assert len(repositories) == 1
        assert len(repositories[0]["open_pull_requests"]) == 2
        assert len(repositories[0]["staging_pull_requests"]) == 1
        assert repositories[0]["staging_pull_requests"][0]["number"] == 1
        assert repositories[0]["local_repo"]["available"] is True
        assert repositories[0]["local_repo"]["current_branch"] == "staging"

    def test_collect_github_ask_context_marks_degraded_when_pr_fetch_fails(self) -> None:
        fake_client = SimpleNamespace(
            list_open_pull_requests=lambda **_kwargs: (_ for _ in ()).throw(GitHubApiError("rate limited"))
        )
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
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

        assert context["available"] is False
        assert context["reason"] == "github_pull_requests_unavailable"
        assert "example/repo" in context["degraded_repositories"]

    def test_collect_github_ask_context_uses_platform_for_unscoped_refs(self) -> None:
        fake_client = SimpleNamespace(list_open_pull_requests=lambda **_kwargs: [])

        def _scoped_secret_lookup(
            session,
            secret_ref: str,
            encryption_key: str,
            tenant_id: str,
            project_id: str | None = None,
        ) -> str | None:
            assert encryption_key == get_settings().secrets_encryption_key
            assert tenant_id == self.tenant_id
            if secret_ref == f"tenant/{self.tenant_id}/GITHUB_APP_ID":
                return "tenant-app-id"
            if secret_ref == f"tenant/{self.tenant_id}/GITHUB_APP_PRIVATE_KEY":
                return "tenant-private-key"
            return None

        def _platform_secret_lookup(session, *, secret_ref: str, encryption_key: str, allow_environment_fallback: bool = True) -> str | None:
            assert encryption_key == get_settings().secrets_encryption_key
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
            assert tenant_secret_lookup is not None
            assert platform_secret_lookup is not None
            assert tenant_secret_lookup(f"tenant/{self.tenant_id}/GITHUB_APP_ID") == "tenant-app-id"
            assert tenant_secret_lookup(f"tenant/{self.tenant_id}/GITHUB_APP_PRIVATE_KEY") == "tenant-private-key"
            assert platform_secret_lookup("GITHUB_APP_ID") == "platform-app-id"
            assert platform_secret_lookup("GITHUB_APP_PRIVATE_KEY") == "platform-private-key"
            return fake_client

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
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

        assert context["available"] is True
        assert len(context["repositories"]) == 1
        assert context["repositories"][0]["repo_full_name"] == "example/repo"

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
        assert create_project.status_code == 201

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
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="answer", command=None),
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="Board answer"),
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

        assert first.ok is True
        assert second.ok is True
        assert collect_calls[0] == ("discord-channel-1", "TP-77")
        assert collect_calls[1] == ("discord-channel-2", None)

    def test_ask_command_surfaces_jira_provider_outage(self) -> None:
        with patch(
            "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
            side_effect=HTTPException(status_code=502, detail="Failed to query Jira board: Bad Gateway"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": "discord-channel-1", "command": "!ask what is blocked"},
            )
        assert response.status_code == 502
        assert "Failed to query Jira board" in response.json()["detail"]

    def test_ask_command_with_dm_channel_uses_tenant_scope(self) -> None:
        with (
            patch(
                "orchestrator.api.discord.ingress.ask_runtime._search_jira_issues_for_tenant",
                return_value=[JiraIssuePreview(key="TP-50", summary="DM issue", status="To Do")],
            ),
            patch(
                "orchestrator.api.discord.commands.ask.plan_discord_ask_intent_with_runtime",
                return_value=AskIntentPayload(mode="answer", summary="answer", command=None),
            ),
            patch("orchestrator.api.discord.commands.ask.answer_board_question_with_runtime", return_value="DM scoped answer"),
        ):
            response = self.client.post(
                f"/discord/command/{self.tenant_id}",
                json={"user_id": "u-viewer", "channel_id": None, "command": "!ask what is on the board"},
            )

        assert response.status_code == 200
        assert response.json()["message"] == "DM scoped answer"

    def test_request_creates_pending_request(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-viewer",
                "channel_id": "discord-channel-1",
                "command": "!request run_controls Need run controls",
            },
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert "Allowlist request" in response.json()["message"]

        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            assert tenant is not None
            project = session.get(Project, f"{self.tenant_id}-default")
            assert project is not None
            requests = (project.discord_config or {}).get("allowlist_requests", [])
            assert len(requests) == 1
            assert requests[0]["user_id"] == "u-viewer"
            assert requests[0]["permissions"] == ["run_controls"]
            assert requests[0]["reason"] == "Need run controls"

    def test_request_for_allowlisted_user_returns_already_allowlisted(self) -> None:
        response = self.client.post(
            f"/discord/command/{self.tenant_id}",
            json={
                "user_id": "u-admin",
                "channel_id": "discord-channel-1",
                "command": "!request run_controls Please add me",
            },
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert "already allowlisted" in response.json()["message"]
