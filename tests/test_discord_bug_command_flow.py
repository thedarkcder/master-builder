from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from orchestrator.api.discord.bug.service import build_discord_bug_description
from orchestrator.api.discord.ingress.bug_runtime import create_discord_bug_issue
from orchestrator.storage.models import AtlassianOAuthConnection, Project, Tenant
from orchestrator.tools.atlassian_oauth import (
    JiraIssueBulkCreateResult,
    JiraIssueCreateResult,
    AtlassianOAuthError,
)
from tests.test_support.discord_command_api_harness import DiscordCommandApiTestHarness


pytestmark = pytest.mark.contract


class DiscordBugCommandFlowTests(DiscordCommandApiTestHarness):
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
                "Bug logged: [TP-501](https://master-builder.atlassian.net/browse/TP-501)",
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

    def test_bug_creation_uploads_discord_attachments_to_jira_issue(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            jira_config = dict(tenant.jira_config)
            jira_config["connection_id"] = "conn-attach"
            tenant.jira_config = jira_config
            session.add(
                AtlassianOAuthConnection(
                    connection_id="conn-attach",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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
                self.upload_calls: list[dict[str, object]] = []

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-901", issue_id="901")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:
                self.upload_calls.append(dict(kwargs))
                return [{"id": "att-1"}]

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_atlassian_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.atlassian_oauth_client", return_value=fake_client),
            patch("orchestrator.api.discord.ingress.bug_runtime.resolve_discord_channel_name", return_value="triage-bugs"),
            patch(
                "orchestrator.api.discord.ingress.bug_runtime.download_discord_attachment",
                return_value=(b"image-bytes", "image/png"),
            ),
        ):
            tenant = session.get(Tenant, self.tenant_id)
            self.assertIsNotNone(tenant)
            message, _data = create_discord_bug_issue(
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
                AtlassianOAuthConnection(
                    connection_id="conn-attach-fail",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-903", issue_id="903")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:
                del kwargs
                raise AtlassianOAuthError("Jira attachment upload failed (403): permission denied")

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_atlassian_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.atlassian_oauth_client", return_value=fake_client),
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
                AtlassianOAuthConnection(
                    connection_id="conn-2",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:
                self.created_issues = list(kwargs.get("issues") or [])
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="TP-902", issue_id="902")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:
                return [{"id": "att-1"}]

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_atlassian_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.atlassian_oauth_client", return_value=fake_client),
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
                AtlassianOAuthConnection(
                    connection_id="conn-3",
                    account_id="acct-1",
                    account_email="dev@example.com",
                    cloud_id="cloud-1",
                    site_url="https://master-builder.atlassian.net",
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

            def create_issues_bulk(self, **kwargs: object) -> JiraIssueBulkCreateResult:
                self.project_key = str(kwargs.get("project_key"))
                return JiraIssueBulkCreateResult(
                    created=[JiraIssueCreateResult(key="OTH-902", issue_id="902")],
                    errors=[],
                )

            def upload_issue_attachment(self, **kwargs: object) -> list[dict]:
                return []

        fake_client = _FakeClient()
        with (
            self.session_factory() as session,
            patch("orchestrator.api.discord.ingress.jira_runtime.refresh_atlassian_connection_tokens", return_value="token"),
            patch("orchestrator.api.discord.ingress.jira_runtime.atlassian_oauth_client", return_value=fake_client),
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
