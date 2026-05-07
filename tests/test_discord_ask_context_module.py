from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from sqlalchemy import select

from orchestrator.api.discord.ask import context as ask_context
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError


class DiscordAskContextModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/ask_context.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)
        self._seed_data()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _seed_data(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={"project_keys": ["LEGACY"]},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config=None,
                created_at=now,
                updated_at=now,
            )
            session.add(tenant)
            session.add_all(
                [
                    Project(
                        project_id="tenant-a-p1",
                        tenant_id="tenant-a",
                        name="P1",
                        github_repository="https://github.com/example/p1",
                        jira_project_key="PA",
                        is_archived=False,
                        created_at=now,
                        updated_at=now,
                    ),
                    Project(
                        project_id="tenant-a-p2",
                        tenant_id="tenant-a",
                        name="P2",
                        github_repository="https://github.com/example/p2",
                        jira_project_key="PB",
                        is_archived=True,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            session.commit()

    def test_normalize_scope_channel_id(self) -> None:
        self.assertIsNone(ask_context._normalize_scope_channel_id(None))
        self.assertIsNone(ask_context._normalize_scope_channel_id(" dm "))
        self.assertIsNone(ask_context._normalize_scope_channel_id("jira:comment"))
        self.assertEqual(ask_context._normalize_scope_channel_id(" 123 "), "123")

    def test_tenant_active_projects_excludes_archived(self) -> None:
        with self.session_factory() as session:
            projects = ask_context.tenant_active_projects(session=session, tenant_id="tenant-a")
            self.assertEqual([project.project_id for project in projects], ["tenant-a-p1"])

    def test_tenant_project_keys_prefers_active_projects_and_falls_back_to_tenant_config(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            assert tenant is not None
            self.assertEqual(ask_context.tenant_project_keys(session=session, tenant=tenant), ["PA"])
            session.execute(select(Project).where(Project.tenant_id == "tenant-a")).scalars().all()[0].is_archived = True
            session.commit()
            self.assertEqual(ask_context.tenant_project_keys(session=session, tenant=tenant), ["LEGACY"])

    def test_project_filter_jql_channel_scope_and_error_paths(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            assert tenant is not None

            scoped = SimpleNamespace(jira_project_key="PA")
            with patch.object(
                ask_context._channel_scope_repository,
                "resolve_project_scope",
                return_value=scoped,
            ):
                self.assertEqual(
                    ask_context.project_filter_jql(session=session, tenant=tenant, channel_id="channel-1"),
                    'project = "PA"',
                )

            with patch.object(
                ask_context._channel_scope_repository,
                "resolve_project_scope",
                return_value=None,
            ):
                with self.assertRaises(HTTPException) as conflict:
                    ask_context.project_filter_jql(session=session, tenant=tenant, channel_id="channel-2")
                self.assertEqual(conflict.exception.status_code, 409)

            # With DM scope, it should use tenant projects.
            self.assertEqual(
                ask_context.project_filter_jql(session=session, tenant=tenant, channel_id="dm"),
                'project = "PA"',
            )

            # Archive all projects and clear tenant keys to trigger no-scope error.
            for project in session.execute(select(Project).where(Project.tenant_id == "tenant-a")).scalars():
                project.is_archived = True
            tenant.jira_config = {"project_keys": []}
            session.commit()
            with self.assertRaises(HTTPException) as bad_req:
                ask_context.project_filter_jql(session=session, tenant=tenant, channel_id="dm")
            self.assertEqual(bad_req.exception.status_code, 400)

    def test_search_jira_issues_for_tenant_success_and_error_paths(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            assert tenant is not None
            fake_client = MagicMock()
            fake_client.search_issues_by_jql.return_value = ["issue-1"]
            oauth = SimpleNamespace(client=fake_client, access_token="token", connection=SimpleNamespace(cloud_id="cloud"))
            with patch.object(ask_context, "tenant_atlassian_oauth_context", return_value=oauth):
                issues = ask_context.search_jira_issues_for_tenant(
                    session=session,
                    tenant=tenant,
                    jql='project = "PA"',
                    max_results=5,
                )
            self.assertEqual(issues, ["issue-1"])

            with patch.object(ask_context, "tenant_atlassian_oauth_context", side_effect=AtlassianOAuthError("upstream")):
                with self.assertRaises(HTTPException) as err:
                    ask_context.search_jira_issues_for_tenant(
                        session=session,
                        tenant=tenant,
                        jql='project = "PA"',
                    )
                self.assertEqual(err.exception.status_code, 502)

    def test_fetch_preview_and_detail_paths(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            assert tenant is not None

            with patch.object(ask_context, "search_jira_issues_for_tenant", return_value=["preview"]):
                preview = ask_context.fetch_jira_issue_preview_for_tenant(
                    session=session,
                    tenant=tenant,
                    issue_key="PA-1",
                )
                self.assertEqual(preview, "preview")

            with patch.object(ask_context, "search_jira_issues_for_tenant", return_value=[]):
                with self.assertRaises(HTTPException) as not_found:
                    ask_context.fetch_jira_issue_preview_for_tenant(
                        session=session,
                        tenant=tenant,
                        issue_key="PA-404",
                    )
                self.assertEqual(not_found.exception.status_code, 404)

            fake_client = MagicMock()
            fake_client.get_issue_detail.return_value = {"key": "PA-1"}
            with (
                patch.object(ask_context, "resolve_tenant_atlassian_connection", return_value=SimpleNamespace(cloud_id="cloud")),
                patch.object(ask_context, "_refresh_atlassian_connection_tokens", return_value="token"),
                patch.object(ask_context, "_atlassian_oauth_client", return_value=fake_client),
            ):
                detail = ask_context.fetch_jira_issue_detail_for_tenant(
                    session=session,
                    tenant=tenant,
                    issue_key="PA-1",
                )
            self.assertEqual(detail, {"key": "PA-1"})

            with (
                patch.object(ask_context, "resolve_tenant_atlassian_connection", return_value=SimpleNamespace(cloud_id="cloud")),
                patch.object(ask_context, "_refresh_atlassian_connection_tokens", side_effect=AtlassianOAuthError("fail")),
            ):
                with self.assertRaises(HTTPException) as bad_gateway:
                    ask_context.fetch_jira_issue_detail_for_tenant(
                        session=session,
                        tenant=tenant,
                        issue_key="PA-1",
                    )
                self.assertEqual(bad_gateway.exception.status_code, 502)

    def test_history_service_wrappers_delegate(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            assert tenant is not None
            with patch.object(ask_context._ask_history_service, "consume_pending_ask_action", return_value={"ok": True}) as consume:
                result = ask_context.consume_pending_ask_action(
                    session=session,
                    tenant=tenant,
                    request_id="req-1",
                )
            self.assertEqual(result, {"ok": True})
            consume.assert_called_once()

            with patch.object(ask_context._ask_history_service, "remove_issue_key_from_ask_history", return_value=3) as remove:
                count = ask_context.remove_issue_key_from_tenant_ask_history(
                    session=session,
                    tenant=tenant,
                    issue_key="PA-1",
                    user_id="u1",
                    channel_id="c1",
                )
            self.assertEqual(count, 3)
            remove.assert_called_once()
