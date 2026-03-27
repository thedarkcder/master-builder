import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Run, Tenant


class TenantUserAccessApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/tenant_user_access.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_ADMIN_USERNAME"] = "admin"
        os.environ["ORCHESTRATOR_ADMIN_PASSWORD"] = "secret"
        os.environ["ORCHESTRATOR_ADMIN_TOKEN_SECRET"] = "admin-token-secret-for-tests-0123456789"
        os.environ["ORCHESTRATOR_AUTH_TOKEN_SECRET"] = "tenant-auth-token-secret-for-tests-0123456789"
        os.environ["ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET"] = "unit-test-secret"
        os.environ["ORCHESTRATOR_ADMIN_UI_BASE_URL"] = "http://localhost:4100"
        os.environ["ORCHESTRATOR_PUBLIC_API_BASE_URL"] = "http://localhost:4000"
        os.environ["ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET"] = "jira-oauth-state-secret"
        os.environ["ORCHESTRATOR_GITHUB_APP_SLUG"] = "master-builder-app"
        os.environ["ORCHESTRATOR_DISCORD_OAUTH_CLIENT_ID"] = "discord-client-id-123"
        os.environ["ORCHESTRATOR_DISCORD_INSTALL_STATE_SECRET"] = "discord-install-state-secret"
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        os.environ["ORCHESTRATOR_CODEX_MODEL"] = "gpt-5.4"
        os.environ["ORCHESTRATOR_CODEX_SUPPORTED_MODELS"] = "gpt-5.4,gpt-5.3-codex,gpt-5.3-codex-spark"

        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        for key in (
            "ORCHESTRATOR_DATABASE_URL",
            "ORCHESTRATOR_ADMIN_USERNAME",
            "ORCHESTRATOR_ADMIN_PASSWORD",
            "ORCHESTRATOR_ADMIN_TOKEN_SECRET",
            "ORCHESTRATOR_AUTH_TOKEN_SECRET",
            "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET",
            "ORCHESTRATOR_ADMIN_UI_BASE_URL",
            "ORCHESTRATOR_PUBLIC_API_BASE_URL",
            "ORCHESTRATOR_JIRA_OAUTH_STATE_SECRET",
            "ORCHESTRATOR_GITHUB_APP_SLUG",
            "ORCHESTRATOR_DISCORD_OAUTH_CLIENT_ID",
            "ORCHESTRATOR_DISCORD_INSTALL_STATE_SECRET",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY",
            "ORCHESTRATOR_CODEX_MODEL",
            "ORCHESTRATOR_CODEX_SUPPORTED_MODELS",
        ):
            os.environ.pop(key, None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _register(self, *, email: str = "owner@example.com", tenant_name: str = "Acme Delivery") -> dict:
        response = self.client.post(
            "/api/public/register",
            json={
                "full_name": "Owner Example",
                "email": email,
                "password": "S3cret-passphrase",
                "tenant_name": tenant_name,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _login(self, *, email: str = "owner@example.com", password: str = "S3cret-passphrase") -> str:
        response = self.client.post(
            "/api/app/auth/login",
            json={
                "email": email,
                "password": password,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["access_token"]

    def _accept_invite(self, *, token: str, password: str = "S3cret-passphrase") -> dict:
        response = self.client.post(
            "/api/public/invites/accept",
            json={
                "token": token,
                "password": password,
                "full_name": "Accepted User",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_public_registration_creates_tenant_admin_membership(self) -> None:
        body = self._register()
        self.assertEqual(body["principal"]["principal_type"], "tenant_user")
        self.assertEqual(len(body["principal"]["memberships"]), 1)
        membership = body["principal"]["memberships"][0]
        self.assertEqual(membership["role"], "tenant_admin")
        self.assertEqual(membership["onboarding_kind"], "tenant_admin_setup")
        self.assertIsNone(membership["onboarding_completed_at"])
        self.assertEqual(membership["effective_mode"], "technical")

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, body["tenant"]["tenant_id"])
            self.assertIsNotNone(tenant)
        self.assertEqual(body["tenant"]["name"], "Acme Delivery")

    def test_tenant_user_login_returns_access_token_and_membership_profile(self) -> None:
        self._register()

        response = self.client.post(
            "/api/app/auth/login",
            json={"email": "owner@example.com", "password": "S3cret-passphrase"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertTrue(payload["access_token"])
        self.assertEqual(payload["principal"]["principal_type"], "tenant_user")
        self.assertEqual(payload["principal"]["memberships"][0]["role"], "tenant_admin")

    def test_tenant_user_can_update_profile_settings_for_tenant(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        profile_response = self.client.put(
            "/api/app/me/profile",
            json={
                "full_name": "Owner Renamed",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(profile_response.status_code, 200, profile_response.text)
        profile_payload = profile_response.json()
        self.assertEqual(profile_payload["full_name"], "Owner Renamed")

        response = self.client.put(
            f"/api/app/tenants/{tenant_id}/me/settings",
            json={
                "mode_override": "non_technical",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["full_name"], "Owner Renamed")
        self.assertEqual(payload["memberships"][0]["mode_override"], "non_technical")

    def test_tenant_user_can_change_password(self) -> None:
        self._register()
        token = self._login()

        response = self.client.post(
            "/api/app/me/password",
            json={
                "current_password": "S3cret-passphrase",
                "new_password": "Changed-pass-456",
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)

        login_response = self.client.post(
            "/api/app/auth/login",
            json={"email": "owner@example.com", "password": "Changed-pass-456"},
        )
        self.assertEqual(login_response.status_code, 200, login_response.text)

    def test_tenant_user_list_tenants_returns_only_memberships(self) -> None:
        first = self._register(email="owner1@example.com", tenant_name="Tenant One")
        self._register(email="owner2@example.com", tenant_name="Tenant Two")
        token = self._login(email="owner1@example.com")

        response = self.client.get(
            "/api/admin/tenants",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        tenant_ids = [item["tenant_id"] for item in response.json()]
        self.assertEqual(tenant_ids, [first["tenant"]["tenant_id"]])

    def test_tenant_admin_can_create_email_invite(self) -> None:
        registration = self._register()
        token = self._login()

        with patch("orchestrator.core.invites.email_delivery.send_tenant_invite_email") as email_mock:
            response = self.client.post(
                f"/api/admin/tenants/{registration['tenant']['tenant_id']}/invites",
                json={
                    "email": "new.user@example.com",
                    "full_name": "New User",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": "non_technical",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["email"], "new.user@example.com")
        self.assertTrue(payload["invite_url"])
        email_mock.assert_called_once()

    def test_tenant_admin_can_create_team(self) -> None:
        registration = self._register()
        token = self._login()

        response = self.client.post(
            f"/api/admin/tenants/{registration['tenant']['tenant_id']}/teams",
            json={
                "name": "Product Ops",
                "description": "Business reporting and delivery tracking",
                "permission_keys": ["analytics.business.view", "runs.business.view"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["name"], "Product Ops")
        self.assertEqual(payload["permission_keys"], ["analytics.business.view", "runs.business.view"])

    def test_tenant_admin_can_list_and_update_members(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        team_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/teams",
            json={
                "name": "Engineering",
                "description": "Technical access",
                "permission_keys": ["analytics.technical.view", "runs.technical.view"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(team_response.status_code, 201, team_response.text)
        team_id = team_response.json()["team_id"]

        list_response = self.client.get(
            f"/api/admin/tenants/{tenant_id}/members",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(list_response.status_code, 200, list_response.text)
        members = list_response.json()
        self.assertEqual(len(members), 1)
        membership_id = members[0]["membership_id"]

        update_response = self.client.put(
            f"/api/admin/tenants/{tenant_id}/members/{membership_id}",
            json={
                "role": "technical_member",
                "team_ids": [team_id],
                "mode_override": "technical",
                "is_active": True,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(update_response.status_code, 200, update_response.text)
        payload = update_response.json()
        self.assertEqual(payload["role"], "technical_member")
        self.assertEqual(payload["team_ids"], [team_id])
        self.assertEqual(payload["mode_override"], "technical")

    def test_tenant_admin_can_resend_and_revoke_invites(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        with patch("orchestrator.core.invites.email_delivery.send_tenant_invite_email") as email_mock:
            create_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites",
                json={
                    "email": "invitee@example.com",
                    "full_name": "Invitee",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": "non_technical",
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(create_response.status_code, 201, create_response.text)
            invite_id = create_response.json()["invite_id"]

            list_response = self.client.get(
                f"/api/admin/tenants/{tenant_id}/invites",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(list_response.status_code, 200, list_response.text)
            self.assertEqual(len(list_response.json()["items"]), 1)

            resend_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites/{invite_id}/resend",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(resend_response.status_code, 200, resend_response.text)
            resent_invite_id = resend_response.json()["invite"]["invite_id"]

            revoke_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites/{resent_invite_id}/revoke",
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(revoke_response.status_code, 200, revoke_response.text)
            self.assertEqual(revoke_response.json()["invite"]["status"], "revoked")
            self.assertEqual(email_mock.call_count, 2)

    def test_public_invite_acceptance_creates_user_and_marks_invite_accepted(self) -> None:
        registration = self._register()
        owner_token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        with patch("orchestrator.core.invites.email_delivery.send_tenant_invite_email") as email_mock:
            create_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites",
                json={
                    "email": "invitee@example.com",
                    "full_name": "Invitee",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": "non_technical",
                },
                headers={"Authorization": f"Bearer {owner_token}"},
            )
            self.assertEqual(create_response.status_code, 201, create_response.text)
            invite_payload = create_response.json()
            invite_token = parse_qs(urlparse(invite_payload["invite_url"]).query)["token"][0]
            invite_id = invite_payload["invite_id"]
            email_mock.assert_called_once()

        accepted = self._accept_invite(token=invite_token, password="Business-pass-123")
        self.assertEqual(accepted["principal"]["email"], "invitee@example.com")
        self.assertEqual(accepted["principal"]["memberships"][0]["tenant_id"], tenant_id)

        session_factory = create_session_factory(self.database_url)
        from orchestrator.storage.models import TenantInvite, TenantUser  # local import keeps test deps scoped

        with session_factory() as session:
            invite = session.get(TenantInvite, invite_id)
            self.assertIsNotNone(invite)
            assert invite is not None
            tenant_user = session.get(TenantUser, invite.accepted_by_user_id)
            self.assertEqual(invite.status, "accepted")
            self.assertIsNotNone(invite.accepted_at)
            self.assertIsNotNone(tenant_user)
            self.assertEqual(tenant_user.email, "invitee@example.com")

    def test_tenant_admin_can_start_discord_install_and_callback_persists_guild(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=wizard",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)
        payload = start_response.json()
        parsed = urlparse(payload["install_url"])
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.netloc, "discord.com")
        self.assertIn("state", query)
        self.assertEqual(query["client_id"], ["discord-client-id-123"])

        callback_response = self.client.get(
            f"/api/admin/discord/install/callback?state={query['state'][0]}&guild_id=987654321&code=oauth-code",
            follow_redirects=False,
        )
        self.assertEqual(callback_response.status_code, 302, callback_response.text)
        self.assertIn("/tenants/new/discord", callback_response.headers["location"])

        tenant_response = self.client.get(
            f"/api/admin/tenants/{tenant_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(tenant_response.status_code, 200, tenant_response.text)
        discord_config = tenant_response.json()["discord"]
        self.assertEqual(discord_config["guild_id"], "987654321")
        self.assertIsNotNone(discord_config["installed_at"])

    def test_non_admin_member_cannot_start_discord_install(self) -> None:
        registration = self._register()
        owner_token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        with patch("orchestrator.core.invites.email_delivery.send_tenant_invite_email") as email_mock:
            create_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites",
                json={
                    "email": "biz@example.com",
                    "full_name": "Biz User",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": "non_technical",
                },
                headers={"Authorization": f"Bearer {owner_token}"},
            )
            self.assertEqual(create_response.status_code, 201, create_response.text)
            invite_url = create_response.json()["invite_url"]
            token = parse_qs(urlparse(invite_url).query)["token"][0]
            email_mock.assert_called_once()

        accepted = self._accept_invite(token=token, password="Business-pass-123")
        member_token = accepted["access_token"]

        response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(response.status_code, 403, response.text)

    def test_onboarding_completion_marks_membership_complete(self) -> None:
        registration = self._register()
        token = self._login()

        response = self.client.post(
            f"/api/app/onboarding/{registration['tenant']['tenant_id']}/complete",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        membership = payload["memberships"][0]
        self.assertIsNotNone(membership["onboarding_completed_at"])

    def test_delivery_summary_counts_completed_blocked_and_failed_runs(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]
        project_id = f"{tenant_id}-project"
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                Project(
                    project_id=project_id,
                    tenant_id=tenant_id,
                    name="Project One",
                    github_repository="https://github.com/example/repo",
                    jira_project_key="ACME",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add_all(
                [
                    Run(
                        run_id="run-succeeded",
                        tenant_id=tenant_id,
                        project_id=project_id,
                        issue_key="ACME-1",
                        issue_summary="Completed story",
                        issue_description=None,
                        repo_url=None,
                        branch=None,
                        pr_url="https://github.com/example/repo/pull/1",
                        dev_session_id=None,
                        pm_session_id=None,
                        orchestrated_session_id=None,
                        dedupe_scope="issue_execution",
                        status="succeeded",
                        last_error=None,
                        plan=None,
                        created_at=now - timedelta(days=2),
                        started_at=now - timedelta(days=2, minutes=-5),
                        last_heartbeat_at=now - timedelta(days=2),
                        worker_service_instance_id=None,
                        finished_at=now - timedelta(days=2, minutes=-20),
                    ),
                    Run(
                        run_id="run-blocked",
                        tenant_id=tenant_id,
                        project_id=project_id,
                        issue_key="ACME-2",
                        issue_summary="Blocked story",
                        issue_description=None,
                        repo_url=None,
                        branch=None,
                        pr_url=None,
                        dev_session_id=None,
                        pm_session_id=None,
                        orchestrated_session_id=None,
                        dedupe_scope="issue_execution",
                        status="blocked",
                        last_error="blocked",
                        plan=None,
                        created_at=now - timedelta(days=1),
                        started_at=now - timedelta(days=1, minutes=-3),
                        last_heartbeat_at=now - timedelta(days=1),
                        worker_service_instance_id=None,
                        finished_at=now - timedelta(days=1, minutes=-7),
                    ),
                    Run(
                        run_id="run-failed",
                        tenant_id=tenant_id,
                        project_id=project_id,
                        issue_key="ACME-3",
                        issue_summary="Failed story",
                        issue_description=None,
                        repo_url=None,
                        branch=None,
                        pr_url=None,
                        dev_session_id=None,
                        pm_session_id=None,
                        orchestrated_session_id=None,
                        dedupe_scope="issue_execution",
                        status="failed",
                        last_error="failed",
                        plan=None,
                        created_at=now - timedelta(hours=12),
                        started_at=now - timedelta(hours=12, minutes=-4),
                        last_heartbeat_at=now - timedelta(hours=12),
                        worker_service_instance_id=None,
                        finished_at=now - timedelta(hours=12, minutes=-11),
                    ),
                ]
            )
            session.commit()

        response = self.client.get(
            f"/api/admin/tenants/{tenant_id}/delivery-summary",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["summary"]["completed_count"], 1)
        self.assertEqual(payload["summary"]["blocked_count"], 1)
        self.assertEqual(payload["summary"]["failed_count"], 1)
        self.assertEqual(len(payload["timeline"]), 3)
