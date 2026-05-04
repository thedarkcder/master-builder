import os
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet

from orchestrator.core.config import get_settings
from orchestrator.core.platform.email_delivery import EmailDeliveryError
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import Project, Run, Tenant, WebhookJob, WorkflowCheckpoint, WorkflowExecution
from orchestrator.tools.discord_api import DiscordApiError
from tests.test_support.db_harness import SqliteTemplateApiTestCase
from tests.workflow_test_support import add_run_with_workflow, add_workflow_attempt, make_run


class TenantUserAccessApiTests(SqliteTemplateApiTestCase):
    _secrets_encryption_key: str
    _baseline_registration: dict
    _baseline_token: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        super().setUpClass()

    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {
            "ORCHESTRATOR_ADMIN_USERNAME": "admin",
            "ORCHESTRATOR_ADMIN_PASSWORD": "secret",
            "ORCHESTRATOR_ADMIN_TOKEN_SECRET": "admin-token-secret-for-tests-0123456789",
            "ORCHESTRATOR_AUTH_TOKEN_SECRET": "tenant-auth-token-secret-for-tests-0123456789",
            "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET": "unit-test-secret",
            "ORCHESTRATOR_ADMIN_UI_BASE_URL": "http://localhost:4100",
            "ORCHESTRATOR_PUBLIC_API_BASE_URL": "http://localhost:4000",
            "ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET": "atlassian-oauth-state-secret",
            "ORCHESTRATOR_GITHUB_APP_SLUG": "master-builder-app",
            "ORCHESTRATOR_DISCORD_OAUTH_CLIENT_ID": "discord-client-id-123",
            "ORCHESTRATOR_DISCORD_INSTALL_STATE_SECRET": "discord-install-state-secret",
            "ORCHESTRATOR_DISCORD_CHANNEL_CATEGORY_ID": "text-category-1",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": cls._secrets_encryption_key,
            "ORCHESTRATOR_CODEX_SUPPORTED_MODELS": "gpt-5.4,gpt-5.4-mini,gpt-5.3-codex",
        }

    @classmethod
    def bootstrap_template_state(cls) -> None:
        registration_response = cls._class_client.post(
            "/api/public/register",
            json={
                "full_name": "Owner Example",
                "email": "owner@example.com",
                "password": "S3cret-passphrase",
                "tenant_name": "Acme Delivery",
            },
        )
        if registration_response.status_code != 201:
            raise AssertionError(
                f"Failed to bootstrap baseline registration: {registration_response.status_code} {registration_response.text}"
            )
        cls._baseline_registration = registration_response.json()
        cls._baseline_token = cls._baseline_registration["access_token"]

    def setUp(self) -> None:
        self.database_url = self._start_test_database(name_prefix="tenant-user-access")

        get_settings.cache_clear()
        reset_db_engine_cache()

    def tearDown(self) -> None:
        self._cleanup_test_database()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _register(self, *, email: str = "owner@example.com", tenant_name: str = "Acme Delivery") -> dict:
        if email == "owner@example.com" and tenant_name == "Acme Delivery":
            return deepcopy(self._baseline_registration)
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
        if email == "owner@example.com" and password == "S3cret-passphrase":
            return self._baseline_token
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

    def test_tenant_user_can_list_runs_for_their_workspace(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                WorkflowExecution(
                    workflow_id="workflow-tenant-user-visible-run",
                    execution_id="workflow-tenant-user-visible-run",
                    workflow_type_key="issue_execution",
                    tenant_id=tenant_id,
                    project_id=f"{tenant_id}-default",
                    source_system="jira",
                    source_ref="TP-101",
                    display_name="Tenant run",
                    source_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="issue_execution",
                    status="queued",
                    last_error=None,
                    active_run_id="tenant-user-visible-run",
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="tenant-user-visible-run",
                    workflow_id="workflow-tenant-user-visible-run",
                    tenant_id=tenant_id,
                    project_id=f"{tenant_id}-default",
                    issue_key="TP-101",
                    issue_summary="Tenant run",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    attempt_number=1,
                    parent_run_id=None,
                    entry_mode="fresh",
                    entry_stage="orchestrated",
                    entry_checkpoint_id=None,
                    dedupe_scope="issue_execution",
                    status="queued",
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None,
                )
            )
            session.commit()

        response = self.client.get(
            f"/api/admin/runs?tenant_id={tenant_id}",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()[0]["run_id"], "tenant-user-visible-run")

    def test_tenant_user_can_list_webhook_jobs_for_project_scope(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]
        default_project_id = f"{tenant_id}-default"
        now = datetime.now(timezone.utc)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            session.add_all(
                [
                    WebhookJob(
                        job_id="tenant-webhook-job-1",
                        transport="github_webhook",
                        tenant_id=tenant_id,
                        project_id=default_project_id,
                        subject_key=f"github_pr:{tenant_id}:repo:11",
                        dedupe_key="webhook-delivery-1",
                        request_id="req-1",
                        event_type="pull_request",
                        status="pending",
                        owner_id=None,
                        lease_expires_at=None,
                        available_at=now,
                        attempt_count=0,
                        last_error=None,
                        payload_json={},
                        context_json={"related_run_id": "tenant-run-11"},
                        created_at=now,
                        updated_at=now,
                        started_at=None,
                        completed_at=None,
                    ),
                    WebhookJob(
                        job_id="tenant-webhook-job-2",
                        transport="jira_webhook",
                        tenant_id=tenant_id,
                        project_id=f"{tenant_id}-other",
                        subject_key=f"jira:{tenant_id}:TP-222",
                        dedupe_key="webhook-delivery-2",
                        request_id="req-2",
                        event_type="jira:issue_updated",
                        status="failed",
                        owner_id=None,
                        lease_expires_at=None,
                        available_at=now,
                        attempt_count=1,
                        last_error="boom",
                        payload_json={},
                        context_json={},
                        created_at=now,
                        updated_at=now,
                        started_at=now,
                        completed_at=now,
                    ),
                ]
            )
            session.commit()

        response = self.client.get(
            (
                f"/api/admin/observability/webhook-jobs?tenant_id={tenant_id}"
                f"&project_id={default_project_id}&limit=25&offset=0"
            ),
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["job_id"], "tenant-webhook-job-1")
        self.assertEqual(payload["items"][0]["related_run_id"], "tenant-run-11")
        self.assertNotIn("owner_id", payload["items"][0])
        self.assertEqual(payload["summary"]["pending_count"], 1)
        self.assertEqual(payload["summary"]["failed_count"], 0)

    def test_tenant_user_can_retry_failed_webhook_jobs_for_project_scope(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]
        default_project_id = f"{tenant_id}-default"
        now = datetime.now(timezone.utc)

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            session.add(
                WebhookJob(
                    job_id="tenant-webhook-job-retry",
                    transport="jira_webhook",
                    tenant_id=tenant_id,
                    project_id=default_project_id,
                    subject_key=f"jira:{tenant_id}:TP-229",
                    dedupe_key="webhook-delivery-retry",
                    request_id="req-retry",
                    event_type="jira:issue_updated",
                    status="failed",
                    owner_id=None,
                    lease_expires_at=None,
                    available_at=now,
                    attempt_count=4,
                    last_error="workflow failed",
                    payload_json={},
                    context_json={"related_run_id": "tenant-run-229"},
                    created_at=now - timedelta(minutes=5),
                    updated_at=now - timedelta(minutes=1),
                    started_at=now - timedelta(minutes=4),
                    completed_at=now - timedelta(minutes=1),
                )
            )
            session.commit()

        response = self.client.post(
            (
                f"/api/admin/observability/webhook-jobs/tenant-webhook-job-retry/retry"
                f"?tenant_id={tenant_id}&project_id={default_project_id}"
            ),
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["job_id"], "tenant-webhook-job-retry")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["related_run_id"], "tenant-run-229")
        self.assertIsNone(payload["last_error"])

    def test_tenant_user_can_list_and_get_workflows_for_their_workspace(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                WorkflowExecution(
                    workflow_id="tenant-user-visible-workflow",
                    execution_id="tenant-user-visible-workflow",
                    workflow_type_key="issue_execution",
                    tenant_id=tenant_id,
                    project_id=f"{tenant_id}-default",
                    source_system="jira",
                    source_ref="TP-111",
                    display_name="Tenant workflow",
                    source_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch="feature/workflow",
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="issue_execution",
                    status="waiting_for_input",
                    last_error=None,
                    active_run_id="tenant-user-workflow-run",
                    latest_checkpoint_id="tenant-user-workflow-checkpoint",
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="tenant-user-workflow-run",
                    workflow_id="tenant-user-visible-workflow",
                    tenant_id=tenant_id,
                    project_id=f"{tenant_id}-default",
                    issue_key="TP-111",
                    issue_summary="Tenant workflow",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch="feature/workflow",
                    pr_url=None,
                    attempt_number=1,
                    parent_run_id=None,
                    entry_mode="fresh",
                    entry_stage="orchestrated",
                    entry_checkpoint_id="tenant-user-workflow-checkpoint",
                    dedupe_scope="issue_execution",
                    status="waiting_for_input",
                    last_error=None,
                    plan=None,
                    created_at=now,
                    started_at=now,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None,
                )
            )
            session.add(
                WorkflowCheckpoint(
                    checkpoint_id="tenant-user-workflow-checkpoint",
                    workflow_id="tenant-user-visible-workflow",
                    run_id="tenant-user-workflow-run",
                    checkpoint_kind="pm",
                    stage="pm",
                    payload_json={"source": "test"},
                    codex_session_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        list_response = self.client.get(
            f"/api/admin/workflows?tenant_id={tenant_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertEqual(list_response.json()[0]["workflow_id"], "tenant-user-visible-workflow")
        self.assertEqual(list_response.json()[0]["runs"][0]["attempt_number"], 1)

        detail_response = self.client.get(
            "/api/admin/workflows/tenant-user-visible-workflow",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(detail_response.status_code, 200, detail_response.text)
        self.assertEqual(detail_response.json()["workflow_id"], "tenant-user-visible-workflow")

    def test_business_member_can_read_runs_without_extra_team_permissions(self) -> None:
        registration = self._register()
        tenant_id = registration["tenant"]["tenant_id"]
        owner_token = self._login()
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            add_run_with_workflow(
                session,
                make_run(
                    run_id="business-member-visible-run",
                    tenant_id=tenant_id,
                    project_id=f"{tenant_id}-default",
                    issue_key="TP-102",
                    issue_summary="Business member run",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    created_at=now,
                    status="queued",
                ),
            )
            session.commit()

        with patch("orchestrator.core.invites.email_delivery.send_tenant_invite_email"):
            invite_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites",
                json={
                    "email": "runs-member@example.com",
                    "full_name": "Runs Member",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": "non_technical",
                },
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        self.assertEqual(invite_response.status_code, 201, invite_response.text)
        invite_token = invite_response.json()["invite_url"].split("token=", 1)[1]
        self._accept_invite(token=invite_token)
        member_token = self._login(email="runs-member@example.com")

        list_response = self.client.get(
            f"/api/admin/runs?tenant_id={tenant_id}&project_id={tenant_id}-default",
            headers={"Authorization": f"Bearer {member_token}"},
        )

        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertEqual(list_response.json()[0]["run_id"], "business-member-visible-run")

        detail_response = self.client.get(
            "/api/admin/runs/business-member-visible-run",
            headers={"Authorization": f"Bearer {member_token}"},
        )

        self.assertEqual(detail_response.status_code, 200, detail_response.text)
        self.assertEqual(detail_response.json()["run_id"], "business-member-visible-run")

    def test_tenant_user_runs_listing_requires_explicit_tenant_scope(self) -> None:
        self._register()
        token = self._login()

        response = self.client.get(
            "/api/admin/runs",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("tenant_id is required", response.json()["detail"])

    def test_tenant_user_workflows_listing_requires_explicit_tenant_scope(self) -> None:
        self._register()
        token = self._login()

        response = self.client.get(
            "/api/admin/workflows",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("tenant_id is required", response.json()["detail"])

    def test_tenant_user_cannot_list_runs_for_other_workspace(self) -> None:
        self._register(email="owner-a@example.com", tenant_name="Workspace A")
        token = self._login(email="owner-a@example.com")
        registration_b = self._register(email="owner-b@example.com", tenant_name="Workspace B")

        response = self.client.get(
            f"/api/admin/runs?tenant_id={registration_b['tenant']['tenant_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertIn(response.status_code, (403, 404), response.text)

    def test_platform_super_admin_can_change_password(self) -> None:
        login_response = self.client.post(
            "/api/admin/auth/login",
            json={"username": "admin", "password": "secret"},
        )
        self.assertEqual(login_response.status_code, 200, login_response.text)
        token = login_response.json()["access_token"]

        change_response = self.client.post(
            "/api/app/me/password",
            json={
                "current_password": "secret",
                "new_password": "New-secret-456",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(change_response.status_code, 200, change_response.text)
        self.assertEqual(change_response.json()["principal_type"], "platform_super_admin")

        old_login_response = self.client.post(
            "/api/admin/auth/login",
            json={"username": "admin", "password": "secret"},
        )
        self.assertEqual(old_login_response.status_code, 401, old_login_response.text)

        new_login_response = self.client.post(
            "/api/admin/auth/login",
            json={"username": "admin", "password": "New-secret-456"},
        )
        self.assertEqual(new_login_response.status_code, 200, new_login_response.text)

    def test_password_reset_request_does_not_leak_email_existence_and_confirm_resets_password(self) -> None:
        self._register()

        with patch("orchestrator.api.routes.app_auth.send_password_reset_email") as email_mock:
            existing_response = self.client.post(
                "/api/public/password-reset/request",
                json={"email": "owner@example.com"},
                headers={"host": "workspace.example.com", "x-forwarded-proto": "https"},
            )
            missing_response = self.client.post(
                "/api/public/password-reset/request",
                json={"email": "missing@example.com"},
                headers={"host": "workspace.example.com", "x-forwarded-proto": "https"},
            )

        self.assertEqual(existing_response.status_code, 202, existing_response.text)
        self.assertEqual(missing_response.status_code, 202, missing_response.text)
        self.assertEqual(existing_response.json(), missing_response.json())
        email_mock.assert_called_once()
        reset_url = email_mock.call_args.kwargs["reset_url"]
        self.assertTrue(reset_url.startswith("https://workspace.example.com/reset-password?token="))
        token = parse_qs(urlparse(reset_url).query)["token"][0]

        confirm_response = self.client.post(
            "/api/public/password-reset/confirm",
            json={"token": token, "new_password": "Reset-pass-456"},
        )
        self.assertEqual(confirm_response.status_code, 200, confirm_response.text)

        old_login_response = self.client.post(
            "/api/app/auth/login",
            json={"email": "owner@example.com", "password": "S3cret-passphrase"},
        )
        self.assertEqual(old_login_response.status_code, 401, old_login_response.text)

        new_login_response = self.client.post(
            "/api/app/auth/login",
            json={"email": "owner@example.com", "password": "Reset-pass-456"},
        )
        self.assertEqual(new_login_response.status_code, 200, new_login_response.text)

        replay_response = self.client.post(
            "/api/public/password-reset/confirm",
            json={"token": token, "new_password": "Another-pass-789"},
        )
        self.assertEqual(replay_response.status_code, 400, replay_response.text)

    def test_discord_onboarding_invite_uses_guild_when_onboarding_channel_is_missing(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            self.assertIsNotNone(tenant)
            assert tenant is not None
            tenant.discord_config = {
                "guild_id": "guild-123",
                "onboarding_channel_id": None,
                "channel_id": None,
                "onboarding_invite_expires_in_seconds": 3600,
                "onboarding_invite_max_uses": 1,
                "notify_events": [],
            }
            session.commit()

        fake_client = Mock()
        fake_client.list_text_channels.return_value = [Mock(channel_id="channel-456")]
        fake_client.create_invite.return_value = {"code": "invite-code"}

        with patch("orchestrator.api.routes.admin_tenants._discord_client", return_value=fake_client):
            response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/discord/onboarding-invite",
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["invite_url"], "https://discord.gg/invite-code")
        fake_client.list_text_channels.assert_called_once_with(guild_id="guild-123")

    def test_invite_email_uses_forwarded_public_host(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        with patch("orchestrator.api.routes.admin_tenants.email_delivery.send_tenant_invite_email") as email_mock:
            response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites",
                json={
                    "email": "new-user@example.com",
                    "full_name": "New User",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": None,
                },
                headers={
                    "Authorization": f"Bearer {token}",
                    "host": "workspace.example.com",
                    "x-forwarded-proto": "https",
                },
            )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["invite_url"].split("?token=")[0], "https://workspace.example.com/invite/accept")
        email_mock.assert_called_once()
        self.assertTrue(email_mock.call_args.kwargs["invite_url"].startswith("https://workspace.example.com/invite/accept?token="))

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

    def test_tenant_admin_create_invite_returns_503_when_email_delivery_fails(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        with patch(
            "orchestrator.api.routes.admin_tenants.email_delivery.send_tenant_invite_email",
            side_effect=EmailDeliveryError("smtp unavailable"),
        ):
            response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites",
                json={
                    "email": "new.user@example.com",
                    "full_name": "New User",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": "non_technical",
                },
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["detail"], "Invite email delivery is unavailable")

        session_factory = create_session_factory(self.database_url)
        from orchestrator.storage.models import TenantInvite

        with session_factory() as session:
            invites = session.query(TenantInvite).filter(TenantInvite.tenant_id == tenant_id).all()
            self.assertEqual(invites, [])

    def test_tenant_admin_can_create_team(self) -> None:
        registration = self._register()
        token = self._login()

        response = self.client.post(
            f"/api/admin/tenants/{registration['tenant']['tenant_id']}/teams",
            json={
                "name": "Product Ops",
                "description": "Business reporting and delivery tracking",
                "permission_keys": ["projects.manage"],
            },
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["name"], "Product Ops")
        self.assertEqual(payload["permission_keys"], ["projects.manage"])

    def test_tenant_admin_can_list_and_update_members(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        team_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/teams",
            json={
                "name": "Engineering",
                "description": "Technical access",
                "permission_keys": ["technical.access"],
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

    def test_business_member_can_list_projects_but_cannot_create_projects(self) -> None:
        registration = self._register()
        tenant_id = registration["tenant"]["tenant_id"]
        owner_token = self._login()
        session_factory = create_session_factory(self.database_url)
        now = datetime.now(timezone.utc)

        with session_factory() as session:
            session.add(
                Project(
                    project_id=f"{tenant_id}-web",
                    tenant_id=tenant_id,
                    name="Route Web",
                    github_repository="https://github.com/example/route-web",
                    jira_project_key="WEB",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with patch("orchestrator.core.invites.email_delivery.send_tenant_invite_email"):
            invite_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites",
                json={
                    "email": "member@example.com",
                    "full_name": "Member Example",
                    "role": "business_member",
                    "team_ids": [],
                    "mode_override": "non_technical",
                },
                headers={"Authorization": f"Bearer {owner_token}"},
            )
        self.assertEqual(invite_response.status_code, 201, invite_response.text)
        token = invite_response.json()["invite_url"].split("token=", 1)[1]
        self._accept_invite(token=token)
        member_token = self._login(email="member@example.com")

        list_response = self.client.get(
            f"/api/admin/tenants/{tenant_id}/projects",
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(list_response.status_code, 200, list_response.text)
        self.assertGreaterEqual(len(list_response.json()), 1)

        forbidden_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/projects",
            json={
                "name": "Forbidden",
                "github_repository": "https://github.com/example/forbidden",
                "jira_project_key": "NOPE",
                "policy_overrides": {},
            },
            headers={"Authorization": f"Bearer {member_token}"},
        )
        self.assertEqual(forbidden_response.status_code, 403, forbidden_response.text)

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

    def test_tenant_admin_resend_invite_returns_503_when_email_delivery_fails(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        with patch("orchestrator.core.invites.email_delivery.send_tenant_invite_email"):
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

        with patch(
            "orchestrator.api.routes.admin_tenants.email_delivery.send_tenant_invite_email",
            side_effect=EmailDeliveryError("smtp unavailable"),
        ):
            resend_response = self.client.post(
                f"/api/admin/tenants/{tenant_id}/invites/{invite_id}/resend",
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(resend_response.status_code, 503, resend_response.text)
        self.assertEqual(resend_response.json()["detail"], "Invite email delivery is unavailable")

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
        self.assertEqual(query["permissions"], ["3224728621023057"])

        with patch("orchestrator.api.routes.admin_discord_install.sync_discord_guild_commands") as sync_mock:
            callback_response = self.client.get(
                f"/api/admin/discord/install/callback?state={query['state'][0]}&guild_id=987654321&code=oauth-code",
                follow_redirects=False,
            )
        self.assertEqual(callback_response.status_code, 302, callback_response.text)
        self.assertIn("/tenants/new/discord", callback_response.headers["location"])
        sync_mock.assert_called_once()

        tenant_response = self.client.get(
            f"/api/admin/tenants/{tenant_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(tenant_response.status_code, 200, tenant_response.text)
        discord_config = tenant_response.json()["discord"]
        self.assertEqual(discord_config["guild_id"], "987654321")
        self.assertIsNotNone(discord_config["installed_at"])

    def test_discord_install_callback_reconciles_default_project_channel_when_setup_is_ready(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            self.assertIsNotNone(tenant)
            assert tenant is not None
            tenant.jira_config = {
                "project_keys": ["TP"],
                "connection_id": "conn-1",
            }
            tenant.repos_config = {
                "allowlist": ["https://github.com/example/repo"],
                "mapping_rules_by_project_key": {"TP": "https://github.com/example/repo"},
                "mapping_rules_by_component": {},
                "fallback_repo": None,
                "github_repository": "https://github.com/example/repo",
            }
            tenant.updated_at = datetime.now(timezone.utc)
            session.commit()

        start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=wizard",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)
        state_token = parse_qs(urlparse(start_response.json()["install_url"]).query)["state"][0]

        with patch(
            "orchestrator.api.admin.route_helpers.resolve_project_discord_channel_binding",
            return_value={"channel_id": "discord-channel-123"},
        ) as resolve_channel_mock:
            callback_response = self.client.get(
                f"/api/admin/discord/install/callback?state={state_token}&guild_id=987654321&code=oauth-code",
                follow_redirects=False,
            )
        self.assertEqual(callback_response.status_code, 302, callback_response.text)
        resolve_channel_mock.assert_called_once()

        with session_factory() as session:
            project = session.get(Project, f"{tenant_id}-default")
            self.assertIsNotNone(project)
            assert project is not None
            self.assertEqual(project.github_repository, "https://github.com/example/repo")
            self.assertEqual(project.jira_project_key, "TP")
            self.assertEqual(project.discord_config["channel_id"], "discord-channel-123")

    def test_discord_install_callback_backfills_live_voice_room_for_existing_project_channel(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            self.assertIsNotNone(tenant)
            assert tenant is not None
            project = Project(
                project_id=f"{tenant_id}-project-1",
                tenant_id=tenant_id,
                name="Alpha Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={"channel_id": "project-text-123"},
                is_archived=False,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(project)
            session.commit()

        start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=edit",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)
        state_token = parse_qs(urlparse(start_response.json()["install_url"]).query)["state"][0]

        fake_client = Mock()
        fake_client.get_channel.return_value = {"id": "project-text-123"}
        fake_client.list_channel_categories.return_value = [
            SimpleNamespace(channel_id="voice-category-1", name="Voice Rooms"),
        ]
        fake_client.list_voice_channels.return_value = []
        fake_client.ensure_voice_channel.return_value = Mock(channel_id="voice-room-123")

        with (
            patch("orchestrator.api.admin.tenant_project_helpers.resolve_platform_secret_ref", return_value="discord-bot-token"),
            patch("orchestrator.api.admin.tenant_project_helpers.DiscordApiClient", return_value=fake_client) as client_mock,
            patch("orchestrator.api.routes.admin_discord_install.sync_discord_guild_commands") as sync_mock,
        ):
            callback_response = self.client.get(
                f"/api/admin/discord/install/callback?state={state_token}&guild_id=987654321&code=oauth-code",
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302, callback_response.text)
        client_mock.assert_called_once_with(bot_token="discord-bot-token")
        fake_client.ensure_text_channel.assert_not_called()
        fake_client.ensure_voice_channel.assert_called_once_with(
            guild_id="987654321",
            name="alpha-project-voice",
            parent_id="voice-category-1",
        )
        sync_mock.assert_called_once()

        with session_factory() as session:
            project = session.get(Project, f"{tenant_id}-project-1")
            self.assertIsNotNone(project)
            assert project is not None
            discord_config = dict(project.discord_config or {})
            self.assertEqual(discord_config["channel_id"], "project-text-123")
            self.assertTrue(discord_config["live_voice_enabled"])
            self.assertEqual(
                discord_config["live_voice_room_links"],
                {"voice-room-123": "project-text-123"},
            )
            self.assertEqual(discord_config["voice_room_channel_ids"], ["voice-room-123"])
            self.assertEqual(discord_config["voice_room_channel_id"], "voice-room-123")

    def test_discord_install_callback_recreates_missing_project_text_channel_before_voice_room(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            tenant = session.get(Tenant, tenant_id)
            self.assertIsNotNone(tenant)
            assert tenant is not None
            project = Project(
                project_id=f"{tenant_id}-project-stale",
                tenant_id=tenant_id,
                name="Beta Project",
                github_repository="https://github.com/example/repo",
                jira_project_key="TP",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={"channel_id": "deleted-text-123"},
                is_archived=False,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(project)
            session.commit()

        start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=edit",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)
        state_token = parse_qs(urlparse(start_response.json()["install_url"]).query)["state"][0]

        fake_client = Mock()
        fake_client.get_channel.side_effect = [DiscordApiError("missing text")]
        fake_client.ensure_text_channel.return_value = Mock(channel_id="project-text-456")
        fake_client.list_channel_categories.return_value = [
            SimpleNamespace(channel_id="voice-category-1", name="Voice Rooms"),
        ]
        fake_client.list_voice_channels.return_value = []
        fake_client.ensure_voice_channel.return_value = Mock(channel_id="voice-room-456")

        with (
            patch("orchestrator.api.admin.tenant_project_helpers.resolve_platform_secret_ref", return_value="discord-bot-token"),
            patch("orchestrator.api.admin.tenant_project_helpers.DiscordApiClient", return_value=fake_client),
            patch("orchestrator.api.routes.admin_discord_install.sync_discord_guild_commands") as sync_mock,
        ):
            callback_response = self.client.get(
                f"/api/admin/discord/install/callback?state={state_token}&guild_id=987654321&code=oauth-code",
                follow_redirects=False,
            )

        self.assertEqual(callback_response.status_code, 302, callback_response.text)
        fake_client.ensure_text_channel.assert_called_once_with(
            guild_id="987654321",
            name="beta-project",
            parent_id="text-category-1",
        )
        fake_client.ensure_voice_channel.assert_called_once_with(
            guild_id="987654321",
            name="beta-project-voice",
            parent_id="voice-category-1",
        )
        sync_mock.assert_called_once()

        with session_factory() as session:
            project = session.get(Project, f"{tenant_id}-project-stale")
            self.assertIsNotNone(project)
            assert project is not None
            discord_config = dict(project.discord_config or {})
            self.assertEqual(discord_config["channel_id"], "project-text-456")
            self.assertEqual(discord_config["live_voice_room_links"], {"voice-room-456": "project-text-456"})

    def test_discord_install_callback_handles_access_denied_without_422(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=wizard",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)
        state_token = parse_qs(urlparse(start_response.json()["install_url"]).query)["state"][0]

        callback_response = self.client.get(
            (
                "/api/admin/discord/install/callback"
                f"?state={state_token}&error=access_denied&error_description=The+resource+owner+rejected+the+request"
            ),
            follow_redirects=False,
        )
        self.assertEqual(callback_response.status_code, 302, callback_response.text)
        redirect_query = parse_qs(urlparse(callback_response.headers["location"]).query)
        self.assertEqual(redirect_query["discord_install"], ["cancelled"])
        self.assertEqual(redirect_query["tenant_id"], [tenant_id])
        self.assertEqual(redirect_query["discord_error"], ["access_denied"])

    def test_discord_install_callback_redirects_edit_mode_to_tenant_settings_section(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=edit",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)
        state_token = parse_qs(urlparse(start_response.json()["install_url"]).query)["state"][0]

        callback_response = self.client.get(
            f"/api/admin/discord/install/callback?state={state_token}&guild_id=987654321&code=oauth-code",
            follow_redirects=False,
        )
        self.assertEqual(callback_response.status_code, 302, callback_response.text)
        self.assertEqual(
            callback_response.headers["location"],
            f"http://localhost:4100/{tenant_id}/settings/discord?discord_install=success",
        )

        cancel_start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=edit",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(cancel_start_response.status_code, 200, cancel_start_response.text)
        cancel_state_token = parse_qs(urlparse(cancel_start_response.json()["install_url"]).query)["state"][0]

        cancel_response = self.client.get(
            (
                "/api/admin/discord/install/callback"
                f"?state={cancel_state_token}&error=access_denied&error_description=Rejected"
            ),
            follow_redirects=False,
        )
        self.assertEqual(cancel_response.status_code, 302, cancel_response.text)
        self.assertEqual(
            cancel_response.headers["location"],
            (
                f"http://localhost:4100/{tenant_id}/settings/discord"
                "?discord_install=cancelled&discord_error=access_denied&discord_error_description=Rejected"
            ),
        )

    def test_discord_install_uses_platform_secret_client_id_when_env_missing(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        os.environ["ORCHESTRATOR_DISCORD_OAUTH_CLIENT_ID"] = ""
        get_settings.cache_clear()

        put_response = self.client.put(
            "/api/admin/secrets/platform%2FDISCORD_OAUTH_CLIENT_ID",
            json={"value": "platform-discord-client-id"},
            auth=("admin", "secret"),
        )
        self.assertEqual(put_response.status_code, 200, put_response.text)

        start_response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/install/start?return_to=wizard",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)
        payload = start_response.json()
        query = parse_qs(urlparse(payload["install_url"]).query)
        self.assertEqual(query["client_id"], ["platform-discord-client-id"])

    def test_discord_identity_reports_oauth_unconfigured_when_redirect_missing(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        os.environ["ORCHESTRATOR_DISCORD_OAUTH_CLIENT_ID"] = ""
        os.environ.pop("ORCHESTRATOR_DISCORD_OAUTH_REDIRECT_URL", None)
        get_settings.cache_clear()

        response = self.client.get(
            f"/api/admin/tenants/{tenant_id}/discord/identity",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertFalse(payload["oauth_configured"])
        self.assertFalse(payload["linked"])

    def test_discord_link_start_returns_conflict_when_oauth_unconfigured(self) -> None:
        registration = self._register()
        token = self._login()
        tenant_id = registration["tenant"]["tenant_id"]

        os.environ["ORCHESTRATOR_DISCORD_OAUTH_CLIENT_ID"] = ""
        os.environ.pop("ORCHESTRATOR_DISCORD_OAUTH_REDIRECT_URL", None)
        get_settings.cache_clear()

        response = self.client.post(
            f"/api/admin/tenants/{tenant_id}/discord/link/start?redirect_to=%2Ftenants%2F{tenant_id}%2Fprofile",
            headers={"Authorization": f"Bearer {token}"},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], "Discord OAuth is not configured")

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
            add_workflow_attempt(
                session,
                run_id="run-succeeded",
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key="ACME-1",
                issue_summary="Completed story",
                issue_description=None,
                repo_url=None,
                pr_url="https://github.com/example/repo/pull/1",
                created_at=now - timedelta(days=2),
                run_status="succeeded",
                workflow_status="succeeded",
                started_at=now - timedelta(days=2, minutes=-5),
                last_heartbeat_at=now - timedelta(days=2),
                finished_at=now - timedelta(days=2, minutes=-20),
            )
            add_workflow_attempt(
                session,
                run_id="run-blocked",
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key="ACME-2",
                issue_summary="Blocked story",
                issue_description=None,
                repo_url=None,
                created_at=now - timedelta(days=1),
                run_status="blocked",
                workflow_status="failed",
                last_error="blocked",
                started_at=now - timedelta(days=1, minutes=-3),
                last_heartbeat_at=now - timedelta(days=1),
                finished_at=now - timedelta(days=1, minutes=-7),
                failure_reason="blocked",
            )
            add_workflow_attempt(
                session,
                run_id="run-failed",
                tenant_id=tenant_id,
                project_id=project_id,
                issue_key="ACME-3",
                issue_summary="Failed story",
                issue_description=None,
                repo_url=None,
                created_at=now - timedelta(hours=12),
                run_status="failed",
                workflow_status="failed",
                last_error="failed",
                started_at=now - timedelta(hours=12, minutes=-4),
                last_heartbeat_at=now - timedelta(hours=12),
                finished_at=now - timedelta(hours=12, minutes=-11),
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
